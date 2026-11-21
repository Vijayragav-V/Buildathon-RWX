# Match the competition's Python and SQLite environment exactly.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock coverage==7.17.4
# Fetch only the allowed, immutable SQLGlot commit. No OS package mirror needed.
RUN python -c "import io,tarfile,urllib.request; data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read(); tarfile.open(fileobj=io.BytesIO(data),mode='r:gz').extractall('/opt',filter='data')" \
    && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot
COPY tools/build_map.py /app/tools/build_map.py
RUN python /app/tools/build_map.py /opt/sqlglot /app/dependencies.json

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /app
COPY --from=build /opt/sqlglot /opt/sqlglot
COPY --from=build /app/dependencies.json /app/dependencies.json
COPY selector.py /app/selector.py
USER 65534:65534
CMD ["python", "/app/selector.py"]
