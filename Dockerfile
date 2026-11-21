# Match the competition's Python and SQLite environment exactly.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock coverage==7.17.4
# Fetch only the allowed, immutable SQLGlot commit. No OS package mirror needed.
RUN python -c "import io,tarfile,urllib.request; data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read(); tarfile.open(fileobj=io.BytesIO(data),mode='r:gz').extractall('/opt',filter='data')" \
    && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot
COPY tools/ /app/tools/
RUN python /app/tools/build_map.py /opt/sqlglot /app/dependencies.json

FROM build AS runtime
ENV HOME=/tmp
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests
COPY value_index.py /app/
RUN PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python /app/value_index.py /opt/sqlglot /app/dependencies.json /app/dependencies.json
COPY replay_costs.py /app/
RUN PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python /app/replay_costs.py /opt/sqlglot /app/dependencies.json
COPY closure_index.py /app/
RUN python /app/closure_index.py /opt/sqlglot /app/dependencies.json
COPY generator_index.py /app/
RUN PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python /app/generator_index.py /opt/sqlglot /app/dependencies.json
COPY selector.py replay.py /app/
USER 65534:65534
CMD ["python", "/app/selector.py"]
