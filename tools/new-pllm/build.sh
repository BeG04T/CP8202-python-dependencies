#!/bin/bash
# Build script for the new-pllm Docker environment

CURRENT_USER=$(whoami)
CURRENT_UID=$(id -u)
CURRENT_GID=$(id -g)

# Determine Docker socket GID (differs between macOS and Linux)
if [ -S /var/run/docker.sock ]; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
        DOCKER_SOCKET_GID=$(stat -f '%g' /var/run/docker.sock)
    else
        DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)
    fi
    echo "Docker socket GID: $DOCKER_SOCKET_GID"
else
    echo "Warning: /var/run/docker.sock not found. Defaulting to GID 999"
    DOCKER_SOCKET_GID=999
fi

echo "Building new-pllm Docker image..."
docker build \
    --build-arg UNAME="$CURRENT_USER" \
    --build-arg UID="$CURRENT_UID" \
    --build-arg GID="$CURRENT_GID" \
    --build-arg DOCKER_GID="$DOCKER_SOCKET_GID" \
    -t new-pllm:latest \
    .

if [ $? -ne 0 ]; then
    echo "Build failed!"
    exit 1
fi

echo ""
echo "Build successful!"
echo ""
echo "To run interactively:"
echo "  docker run -it --rm \\"
echo "    -v /var/run/docker.sock:/var/run/docker.sock:rw \\"
echo "    -v \$(pwd):/app \\"
echo "    --name new-pllm-runner \\"
echo "    new-pllm:latest"
echo ""
echo "Or use docker-compose:"
echo "  docker-compose up -d"
echo ""

