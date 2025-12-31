#!/bin/bash

# Build script for ABS-KoSync Enhanced
# Usage: ./build.sh [version]

VERSION=${1:-latest}
IMAGE_NAME="abs-kosync-enhanced"

echo "🔨 Building $IMAGE_NAME:$VERSION"
echo ""

# Build the image
docker build -t $IMAGE_NAME:$VERSION .

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Build successful!"
    echo ""
    echo "Run with:"
    echo "  docker compose up -d"
    echo ""
    echo "Or tag and push:"
    echo "  docker tag $IMAGE_NAME:$VERSION your-username/$IMAGE_NAME:$VERSION"
    echo "  docker push your-username/$IMAGE_NAME:$VERSION"
else
    echo ""
    echo "❌ Build failed!"
    exit 1
fi
