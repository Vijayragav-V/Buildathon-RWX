# Match the competition's Python and SQLite environment exactly.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock coverage==7.17.4
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
# Fetch only the allowed, immutable SQLGlot commit. No OS package mirror needed.
RUN python -c "import io,tarfile,urllib.request; data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_COMMIT}.tar.gz').read(); tarfile.open(fileobj=io.BytesIO(data),mode='r:gz').extractall('/opt',filter='data')" \
    && mv "/opt/sqlglot-${SQLGLOT_COMMIT}" /opt/sqlglot
ENV HOME=/tmp
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests
COPY evidence/ /app/evidence/
RUN python evidence/install.py evidence/dependencies.sqlite.xz dependencies.sqlite "$SQLGLOT_COMMIT" requirements.lock
COPY tools/ /app/tools/
COPY value_index.py replay_costs.py closure_index.py generator_index.py child_observers.py dependency_store.py entry_index.py /app/
COPY selector.py replay.py patch_semantics.py /app/
USER 65534:65534
CMD ["python", "/app/selector.py"]
