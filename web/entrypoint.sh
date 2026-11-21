#!/bin/sh
set -e

: "${CREWAI_API_KEY:?CREWAI_API_KEY is required}"

sed -i "s|__CREWAI_API_KEY__|${CREWAI_API_KEY}|g" /etc/nginx/conf.d/default.conf

exec nginx -g 'daemon off;'
