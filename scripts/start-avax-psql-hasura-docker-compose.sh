#!/bin/bash

# requirements:
# 	docker
# 	git
# 	grep
# 	awk
# 	sh
# 	rm	
# 	sed

DOCKER_COMPOSE_VERSION="1.26.2"

# Install Docker
function installdocker() {
    sudo apt-get update -y
    sudo apt-get install -y \
        apt-transport-https \
        ca-certificates \
        curl \
        gnupg-agent \
        software-properties-common

    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo apt-key add - 
    sudo apt-key fingerprint 0EBFCD88
    sudo add-apt-repository -y \
        "deb [arch=amd64] https://download.docker.com/linux/ubuntu \
        $(lsb_release -cs) \
        stable"
    sudo apt-get -y update
    sudo apt-get -y install docker-ce docker-ce-cli containerd.io

    sudo curl -L "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
    sudo ln -s /usr/local/bin/docker-compose /usr/bin/docker-compose
}

read -p 'Install Docker? [y/n] ' inst_docker
if [ $inst_docker = "y" ]; then
   installdocker
else
  echo "Not installing Docker..."
fi

read -p 'Build avalanchego? [y/n] ' avax
if [ $avax = "y" ]; then
   #clone avax
	echo "cloning avax"
	git clone https://github.com/ava-labs/avalanchego.git >/dev/null 2>&1

	#build docker image
	echo "building docker image"
	sh avalanchego/scripts/build_image.sh >/dev/null 2>&1

	#delete cloned git folder
	echo "remove avax folder"
	rm -rf avalanchego >/dev/null 2>&1
else
  echo "Not building avalanchego..."
fi

#echo image with tag
echo "docker image:"
VAR=$(docker images | grep avaplatform/avalanchego | awk '{print $1":"$2}')
echo $VAR

#replace in template docker image
echo "generating docker-compose.yaml"
sed "s~AVAX_IMAGE~$VAR~g" template-avax-psql-hasura.yaml > docker-compose.yaml


read -p 'Run docker-compose up? [y/n] ' compose
if [ $compose = "y" ]; then
   docker-compose up
else
  echo "Not running docker-compose"
fi