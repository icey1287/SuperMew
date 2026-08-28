from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.core.errors import AppError, ErrorCode
from backend.db.models import ModelAssignment, ModelProfile, User, utcnow
from backend.infra.database import SessionLocal
from backend.model_control.contracts import (
    MODEL_ROLE_REQUIREMENTS,
    ModelProfileRecord,
    ModelRole,
)


SessionFactory = Callable[[], Session]


class ModelControlRepository:
    """Persist Model Profiles and role Assignments behind one transaction seam."""

    def __init__(self, session_factory: SessionFactory = SessionLocal) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _record(profile: ModelProfile) -> ModelProfileRecord:
        return ModelProfileRecord(
            id=profile.id,
            display_name=profile.display_name,
            provider=profile.provider,
            model_name=profile.model_name,
            base_url=profile.base_url,
            timeout_seconds=float(profile.timeout_seconds),
            supports_stream=profile.supports_stream,
            supports_structured_output=profile.supports_structured_output,
            enabled=profile.enabled,
            source=profile.source,
            version=profile.version,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )

    @staticmethod
    def _user_id(db: Session, username: str | None) -> int | None:
        if not username:
            return None
        row = db.query(User.id).filter(User.username == username).first()
        if row is None:
            raise AppError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                "用户不存在或已失效",
                status_code=401,
            )
        return int(row[0])

    def list_profiles(self) -> tuple[ModelProfileRecord, ...]:
        db = self._session_factory()
        try:
            rows = (
                db.query(ModelProfile)
                .order_by(ModelProfile.display_name.asc(), ModelProfile.id.asc())
                .all()
            )
            return tuple(self._record(row) for row in rows)
        finally:
            db.close()

    def get_profile(self, profile_id: str) -> ModelProfileRecord:
        db = self._session_factory()
        try:
            row = db.query(ModelProfile).filter(ModelProfile.id == profile_id).first()
            if row is None:
                raise AppError(
                    ErrorCode.NOT_FOUND,
                    "Model Profile 不存在",
                    status_code=404,
                    category="model",
                    stage="catalog",
                )
            return self._record(row)
        finally:
            db.close()

    def find_profile(
        self,
        *,
        provider: str,
        model_name: str,
        base_url: str,
    ) -> ModelProfileRecord | None:
        db = self._session_factory()
        try:
            row = (
                db.query(ModelProfile)
                .filter(
                    ModelProfile.provider == provider,
                    ModelProfile.model_name == model_name,
                    ModelProfile.base_url == base_url,
                )
                .order_by(ModelProfile.created_at.asc())
                .first()
            )
            return self._record(row) if row is not None else None
        finally:
            db.close()

    def create_profile(
        self,
        *,
        display_name: str,
        provider: str,
        model_name: str,
        base_url: str,
        timeout_seconds: float,
        supports_stream: bool,
        supports_structured_output: bool,
        enabled: bool,
        source: str,
        username: str | None,
    ) -> ModelProfileRecord:
        db = self._session_factory()
        try:
            now = utcnow()
            profile = ModelProfile(
                id=f"model_{uuid4().hex}",
                display_name=display_name,
                provider=provider,
                model_name=model_name,
                base_url=base_url,
                timeout_seconds=Decimal(str(timeout_seconds)),
                supports_stream=bool(supports_stream),
                supports_structured_output=bool(supports_structured_output),
                enabled=bool(enabled),
                source=source,
                version=1,
                created_by_user_id=self._user_id(db, username),
                created_at=now,
                updated_at=now,
            )
            db.add(profile)
            db.commit()
            db.refresh(profile)
            return self._record(profile)
        except IntegrityError as exc:
            db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                "Model Profile 名称已存在",
                status_code=409,
                category="model",
                stage="catalog",
            ) from exc
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def update_profile(
        self,
        *,
        profile_id: str,
        display_name: str,
        provider: str,
        model_name: str,
        base_url: str,
        timeout_seconds: float,
        supports_stream: bool,
        supports_structured_output: bool,
        enabled: bool,
        username: str,
    ) -> ModelProfileRecord:
        db = self._session_factory()
        try:
            with db.begin():
                self._user_id(db, username)
                profile = (
                    db.query(ModelProfile)
                    .filter(ModelProfile.id == profile_id)
                    .with_for_update()
                    .first()
                )
                if profile is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "Model Profile 不存在",
                        status_code=404,
                        category="model",
                        stage="catalog",
                    )
                assigned_roles = {
                    ModelRole(row.role)
                    for row in db.query(ModelAssignment)
                    .filter(ModelAssignment.profile_id == profile.id)
                    .all()
                }
                for role in assigned_roles:
                    requirement = MODEL_ROLE_REQUIREMENTS[role]
                    if requirement.supports_stream and not supports_stream:
                        raise AppError(
                            ErrorCode.CONFLICT,
                            f"{role.value} 角色要求模型支持流式输出",
                            status_code=409,
                            category="model",
                            stage="assignment",
                        )
                    if (
                        requirement.supports_structured_output
                        and not supports_structured_output
                    ):
                        raise AppError(
                            ErrorCode.CONFLICT,
                            f"{role.value} 角色要求模型支持结构化输出",
                            status_code=409,
                            category="model",
                            stage="assignment",
                        )
                if assigned_roles and not enabled:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "已分配角色的 Model Profile 不能停用",
                        status_code=409,
                        category="model",
                        stage="assignment",
                    )
                profile.display_name = display_name
                profile.provider = provider
                profile.model_name = model_name
                profile.base_url = base_url
                profile.timeout_seconds = Decimal(str(timeout_seconds))
                profile.supports_stream = bool(supports_stream)
                profile.supports_structured_output = bool(supports_structured_output)
                profile.enabled = bool(enabled)
                profile.version += 1
                profile.updated_at = utcnow()
                db.flush()
                return self._record(profile)
        except IntegrityError as exc:
            db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                "Model Profile 名称已存在",
                status_code=409,
                category="model",
                stage="catalog",
            ) from exc
        finally:
            db.close()

    def delete_profile(self, *, profile_id: str, username: str) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                self._user_id(db, username)
                profile = (
                    db.query(ModelProfile)
                    .filter(ModelProfile.id == profile_id)
                    .with_for_update()
                    .first()
                )
                if profile is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "Model Profile 不存在",
                        status_code=404,
                        category="model",
                        stage="catalog",
                    )
                assigned = (
                    db.query(ModelAssignment.role)
                    .filter(ModelAssignment.profile_id == profile.id)
                    .first()
                )
                if assigned is not None:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "已分配角色的 Model Profile 不能删除",
                        status_code=409,
                        category="model",
                        stage="assignment",
                    )
                db.delete(profile)
        finally:
            db.close()

    def assignments(self) -> dict[ModelRole, ModelProfileRecord]:
        db = self._session_factory()
        try:
            rows = (
                db.query(ModelAssignment, ModelProfile)
                .join(ModelProfile, ModelProfile.id == ModelAssignment.profile_id)
                .all()
            )
            return {
                ModelRole(assignment.role): self._record(profile)
                for assignment, profile in rows
            }
        finally:
            db.close()

    def assign(
        self,
        *,
        role: ModelRole,
        profile_id: str,
        username: str | None,
    ) -> ModelProfileRecord:
        requirement = MODEL_ROLE_REQUIREMENTS[role]
        db = self._session_factory()
        try:
            with db.begin():
                user_id = self._user_id(db, username)
                profile = (
                    db.query(ModelProfile)
                    .filter(ModelProfile.id == profile_id)
                    .with_for_update()
                    .first()
                )
                if profile is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "Model Profile 不存在",
                        status_code=404,
                        category="model",
                        stage="assignment",
                    )
                if not profile.enabled:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "停用的 Model Profile 不能分配角色",
                        status_code=409,
                        category="model",
                        stage="assignment",
                    )
                if requirement.supports_stream and not profile.supports_stream:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        f"{role.value} 角色要求模型支持流式输出",
                        status_code=409,
                        category="model",
                        stage="assignment",
                    )
                if (
                    requirement.supports_structured_output
                    and not profile.supports_structured_output
                ):
                    raise AppError(
                        ErrorCode.CONFLICT,
                        f"{role.value} 角色要求模型支持结构化输出",
                        status_code=409,
                        category="model",
                        stage="assignment",
                    )
                assignment = (
                    db.query(ModelAssignment)
                    .filter(ModelAssignment.role == role.value)
                    .with_for_update()
                    .first()
                )
                if assignment is None:
                    assignment = ModelAssignment(
                        role=role.value,
                        profile_id=profile.id,
                        updated_by_user_id=user_id,
                        updated_at=utcnow(),
                    )
                    db.add(assignment)
                else:
                    assignment.profile_id = profile.id
                    assignment.updated_by_user_id = user_id
                    assignment.updated_at = utcnow()
                db.flush()
                return self._record(profile)
        finally:
            db.close()


__all__ = ["ModelControlRepository"]
