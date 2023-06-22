#!/usr/bin/env bash

set -o pipefail || true
set -eu

OWNER="${1:-}"
if [ 'x' = "x$OWNER" ]; then
  echo 'no owner!'
  exit 1
fi
shift

REFERENCE="${2:-}"
if [ 'x' = "x$REFERENCE" ]; then
  echo 'no REFERENCE!'
  exit 1
fi
shift
IMAGES="${@:-}"
if [ 'x' = "x$IMAGES" ]; then
  echo 'no images to upload'
  exit 1
fi
VERSION=$(echo "$REFERENCE" | sed -e 's,.*/\(.*\),\1,')
echo "$OWNER -> $REFERENCE -> $VERSION for ${IMAGES}"

for IMAGE_NAME in $IMAGES
do
    if ! case "${IMAGE_NAME}" in *-test) true;; *) false;; esac; then
      IMAGE_ID="ghcr.io/${OWNER}/$IMAGE_NAME"
      echo "Uploading $IMAGE_NAME to $IMAGE_ID"

      # Change all uppercase to lowercase
      IMAGE_ID=$(echo $IMAGE_ID | tr '[A-Z]' '[a-z]')
      # Strip git ref prefix from version
      # Strip "v" prefix from tag name
      if [[ "$REFERENCE" == "refs/tags/"* ]]; then
        VERSION=$(echo $VERSION | sed -e 's/^v//')
        echo "Detected tag reference, setting version to ${VERSION}"
      fi
      # Use Docker `latest` tag convention
      [ "$VERSION" == "main" ] && VERSION=latest
      echo IMAGE_ID=$IMAGE_ID
      echo VERSION=$VERSION
      docker tag $IMAGE_NAME $IMAGE_ID:$VERSION
      docker push $IMAGE_ID:$VERSION
    else
      echo "Skipping ${IMAGE_NAME} for upload"
    fi
done
