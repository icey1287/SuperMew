"""Deep Web Research Module contracts and SSRF policy seam."""

from backend.web_research.contracts import (
    DEFAULT_WEB_RESEARCH_LIMITS,
    WebCitation,
    WebEvidence,
    WebResearchContractCode,
    WebResearchContractError,
    WebResearchLimits,
    WebResearchQuery,
    WebResearchResult,
)
from backend.web_research.url_policy import (
    CancellationProbe,
    DnsPinSnapshot,
    ResolvedWebUrl,
    SystemWebDnsResolver,
    WebDnsResolver,
    WebUrlPolicy,
    WebUrlPolicyCode,
    WebUrlPolicyError,
)


__all__ = [
    "DEFAULT_WEB_RESEARCH_LIMITS",
    "CancellationProbe",
    "DnsPinSnapshot",
    "ResolvedWebUrl",
    "SystemWebDnsResolver",
    "WebCitation",
    "WebDnsResolver",
    "WebEvidence",
    "WebResearchContractCode",
    "WebResearchContractError",
    "WebResearchLimits",
    "WebResearchQuery",
    "WebResearchResult",
    "WebUrlPolicy",
    "WebUrlPolicyCode",
    "WebUrlPolicyError",
]
