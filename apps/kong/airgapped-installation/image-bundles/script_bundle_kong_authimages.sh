set -euo pipefail

BUNDLE_DIR="$(pwd)/shush-kong"
mkdir -p "$BUNDLE_DIR"
BUNDLE_NAME="ts43-images-$(date +%Y%m%d)"
ARCH="linux/amd64"   # your nodes are x86_64

# Pull all images
while read -r img; do
  [[ -z "$img" ]] && continue
  echo "Pulling $img ..."
  docker pull --platform="$ARCH" "$img"
done < images.txt

# Save ALL images into one tar, then gzip it
echo "Saving combined archive ..."
docker save $(cat images.txt | xargs) -o "$BUNDLE_DIR/$BUNDLE_NAME.tar"
gzip -f "$BUNDLE_DIR/$BUNDLE_NAME.tar"

# (Optional) also save per-image tars
mkdir -p "$BUNDLE_DIR/per-image"
while read -r img; do
  [[ -z "$img" ]] && continue
  safe="$(echo "$img" | sed 's|/|-|g; s|:|__|g')"
  docker save "$img" -o "$BUNDLE_DIR/per-image/${safe}.tar"
done < images.txt

# Checksums + manifest
( cd "$BUNDLE_DIR" && shasum -a 256 * || sha256sum * ) > "$BUNDLE_DIR/SHA256SUMS.txt" #added checksum 
cp images.txt "$BUNDLE_DIR/manifest.txt"

echo "Bundle ready: $BUNDLE_DIR/${BUNDLE_NAME}.tar.gz"
