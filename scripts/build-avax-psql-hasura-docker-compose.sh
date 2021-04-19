# requirements:
# 	docker
# 	git
# 	grep
# 	awk
# 	sh
# 	rm	
# 	sed

#clone avax
echo "cloning avax"
git clone https://github.com/ava-labs/avalanchego.git >/dev/null 2>&1

#build docker image
echo "building docker image"
sh avalanchego/scripts/build_image.sh >/dev/null 2>&1

#delete cloned git folder
echo "remove avax folder"
rm -rf avalanchego >/dev/null 2>&1

#echo image with tag
echo "docker image:"
VAR=$(docker images | grep avaplatform/avalanchego | awk '{print $1":"$2}')
echo $VAR

#replace in template docker image
echo "generating docker-compose"
sed "s~AVAX_IMAGE~$VAR~g" template-avax-psql-hasura.yaml > docker-compose.yaml
