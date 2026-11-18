#!/usr/bin/with-contenv bashio
# Start the Villa Agent with the add-on options. Secrets are passed as environment variables
# to the agent process only; they are never printed.
set -e
site_id="$(bashio::config 'site_id')"
mkdir -p /data/site
site_file="/data/site/${site_id}.yaml"
if bashio::config.has_value 'site_file'; then
  bashio::config 'site_file' > "${site_file}"
elif [ -f "/homeassistant/villa_os/${site_id}.yaml" ]; then
  cp "/homeassistant/villa_os/${site_id}.yaml" "${site_file}"
else
  bashio::exit.nok "No site file. Put it in /config/villa_os/${site_id}.yaml or paste it in 'site_file'."
fi
bashio::config 'mqtt_ca' | sed 's/\\n/\n/g' > /data/ca.crt
grep -q "BEGIN CERTIFICATE" /data/ca.crt || bashio::exit.nok "mqtt_ca must be the CA certificate printed by make add-site."

export SITE_FILE="${site_file}"
export HA_URL="ws://supervisor/core/websocket"
export HA_TOKEN="${SUPERVISOR_TOKEN}"
export MQTT_HOST="$(bashio::config 'mqtt_host')"
export MQTT_PORT="$(bashio::config 'mqtt_port')"
export MQTT_TLS=true
export MQTT_CA_FILE=/data/ca.crt
export MQTT_USERNAME="$(bashio::config 'mqtt_username')"
export MQTT_PASSWORD="$(bashio::config 'mqtt_password')"
export QUEUE_PATH=/data/queue.sqlite
export COMMANDS_DB=/data/commands.sqlite
export LOG_LEVEL="$(bashio::config 'log_level')"
if bashio::config.has_value 'command_secret'; then
  export COMMAND_SECRET="$(bashio::config 'command_secret')"
fi
export COMMANDS_ENABLED="$(bashio::config 'commands_enabled')"
bashio::log.info "Starting Villa Agent for ${site_id} (commands: ${COMMANDS_ENABLED})"
exec python3 -m villa_agent run
