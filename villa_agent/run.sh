#!/usr/bin/with-contenv bashio
# Start the Villa Agent. The only setting is the connection code from Villa OS.
# Secrets go to the agent process as environment variables; they are never printed.
set -e
code="$(bashio::config 'connection_code')"
if [ -z "${code}" ]; then
  bashio::exit.nok "Paste the connection code from Villa OS in the Configuration tab, then Save and Start."
fi
mkdir -p /data/site
exports="$(printf '%s' "${code}" | python3 -m villa_agent connection-env --ca-out /data/ca.crt)" \
  || bashio::exit.nok "The connection code is not right (see the line above). Copy it again from Villa OS."
eval "${exports}"

export HA_URL="ws://supervisor/core/websocket"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

# Devices: your own file if you made one, otherwise picked automatically on every start.
own="/homeassistant/villa_os/${SITE_ID}.yaml"
site_file="/data/site/${SITE_ID}.yaml"
if [ -f "${own}" ]; then
  cp "${own}" "${site_file}"
  bashio::log.info "Using your device list: /config/villa_os/${SITE_ID}.yaml"
else
  bashio::log.info "Finding the devices of this house in Home Assistant..."
  python3 -m villa_agent autosite --site-id "${SITE_ID}" --display-name "${SITE_NAME}" \
    --out "${site_file}" || bashio::exit.nok "Could not build the device list (see the line above)."
fi

export SITE_FILE="${site_file}"
export QUEUE_PATH=/data/queue.sqlite
export COMMANDS_DB=/data/commands.sqlite
export LOG_LEVEL="$(bashio::config 'log_level')"
export COMMANDS_ENABLED="$(bashio::config 'commands_enabled')"
bashio::log.info "Starting Villa Agent for ${SITE_ID} -> ${MQTT_HOST}:${MQTT_PORT} (commands: ${COMMANDS_ENABLED})"
exec python3 -m villa_agent run
