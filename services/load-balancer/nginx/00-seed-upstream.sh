#!/bin/sh
# Seed the shared nginx-conf volume with a default upstream.conf on first start.
# The lb-sidecar will overwrite this file once it receives routing signals.
if [ ! -f /etc/nginx/conf.d/upstream.conf ]; then
    cp /etc/nginx/upstream.conf.default /etc/nginx/conf.d/upstream.conf
fi
