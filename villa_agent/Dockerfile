ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM $BUILD_FROM
# agent/ is copied in by `make addon` (it is the same code as the agent/ folder).
COPY agent/ /opt/villa-agent/
RUN pip install --no-cache-dir /opt/villa-agent && rm -rf /root/.cache
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
