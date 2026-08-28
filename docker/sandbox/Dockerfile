ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}

COPY --chmod=0555 runner.py /opt/supermew/runner.py

RUN mkdir -p /workspace \
    && chown 65532:65532 /workspace \
    && chmod 0700 /workspace

USER 65532:65532
WORKDIR /workspace

ENV HOME=/workspace \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1

ENTRYPOINT ["/usr/local/bin/python", "-I", "-B", "/opt/supermew/runner.py"]
