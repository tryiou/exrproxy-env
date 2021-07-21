#!/bin/bash

cat > /opt/uwsgi/conf/uwsgi.ini << EOL
[uwsgi]
processes = 4
threads = 4

buffer-size = 32768

set-ph = SERVICENODE_PRIVKEY=${SN_KEY}

set-ph = BLOCKNET_CHAIN=mainnet
set-ph = PLUGINS=eth_passthrough

set-ph = HANDLE_PAYMENTS=true
set-ph = HANDLE_PAYMENTS_ENFORCE=true
set-ph = HANDLE_PAYMENTS_RPC_INCLUDE_HEADERS=true
set-ph = HANDLE_PAYMENTS_RPC_HOSTIP=snode
set-ph = HANDLE_PAYMENTS_RPC_PORT=41414
set-ph = HANDLE_PAYMENTS_RPC_USER=${RPC_USER}
set-ph = HANDLE_PAYMENTS_RPC_PASS=${RPC_PASSWORD}
set-ph = HANDLE_PAYMENTS_RPC_VER=2.0

set-ph = RPC_BLOCK_HOSTIP=snode
set-ph = RPC_BLOCK_PORT=41414
set-ph = RPC_BLOCK_USER=${RPC_USER}
set-ph = RPC_BLOCK_PASS=${RPC_PASSWORD}
set-ph = RPC_BLOCK_VER=2.0

set-ph = RPC_BTC_HOSTIP=BTC
set-ph = RPC_BTC_PORT=8332
set-ph = RPC_BTC_USER=${RPC_USER}
set-ph = RPC_BTC_PASS=${RPC_PASSWORD}
set-ph = RPC_BTC_VER=2.0

set-ph = RPC_LTC_HOSTIP=LTC
set-ph = RPC_LTC_PORT=9332
set-ph = RPC_LTC_USER=${RPC_USER}
set-ph = RPC_LTC_PASS=${RPC_PASSWORD}
set-ph = RPC_LTC_VER=2.0

EOL
# ensure supervisord runs at pid1
exec supervisord -c /etc/supervisord.conf