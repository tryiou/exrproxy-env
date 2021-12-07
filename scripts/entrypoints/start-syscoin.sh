#!/bin/sh

cat > /opt/blockchain/config/syscoin.conf << EOL
datadir=/opt/blockchain/data
rpcbind=0.0.0.0
rpcuser=${RPC_USER}
rpcpassword=${RPC_PASSWORD}
rpcallowip=172.31.0.0/20

server=1
listen=1
rpcuser=
rpcpassword=
rpcallowip=0.0.0.0/0
port=8369
rpcport=8370
txindex=1


# Legacy addresses must be used
addresstype=legacy
changetype=legacy


 
# Enable deprecated calls
deprecatedrpc=addresses




EOL

# ensure docker runs daemon as pid1
exec $1 -conf=/opt/blockchain/config/syscoin.conf