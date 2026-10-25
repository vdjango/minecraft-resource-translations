#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')}"
ARCH="${DEB_ARCH:-all}"
OUT_DIR="${ROOT_DIR}/dist"
PACKAGE="${OUT_DIR}/buggcraft-translation_${VERSION}_${ARCH}.deb"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/DEBIAN" "$STAGE/opt/buggcraft-translation/app" "$STAGE/var/lib/buggcraft-translation"
cp -R "$ROOT_DIR/src" "$ROOT_DIR/deploy" "$ROOT_DIR/pyproject.toml" "$ROOT_DIR/LICENSE" "$ROOT_DIR/.env.example" "$STAGE/opt/buggcraft-translation/app/"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: buggcraft-translation
Version: ${VERSION}
Section: web
Priority: optional
Architecture: ${ARCH}
Maintainer: Buggcraft contributors
Depends: python3 (>= 3.11), python3-venv
Description: Buggcraft Minecraft resource translation platform
 Shared, versioned translations with an asynchronous model worker and
 optional federation mirrors.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -eu
APP=/opt/buggcraft-translation/app
VENV=/opt/buggcraft-translation/venv
DATA=/var/lib/buggcraft-translation
if ! getent group buggtranslate >/dev/null; then groupadd --system buggtranslate; fi
if ! getent passwd buggtranslate >/dev/null; then useradd --system --gid buggtranslate --home-dir "$DATA" --no-create-home buggtranslate; fi
mkdir -p "$DATA"
chown -R buggtranslate:buggtranslate "$DATA"
if [ ! -x "$VENV/bin/python" ]; then python3 -m venv "$VENV"; fi
"$VENV/bin/python" -m pip install --no-cache-dir --upgrade "$APP"
if [ ! -f /etc/buggcraft-translation.env ]; then
  ADMIN_TOKEN=$(python3 -c 'import secrets; print("bct_admin_" + secrets.token_urlsafe(32))')
  sed -e 's|TRANSLATION_HOST=.*|TRANSLATION_HOST=127.0.0.1|' \
      -e 's|TRANSLATION_DATABASE=.*|TRANSLATION_DATABASE=/var/lib/buggcraft-translation/translations.sqlite3|' \
      -e 's|TRANSLATION_ADMIN_TOKEN=.*|TRANSLATION_ADMIN_TOKEN='"$ADMIN_TOKEN"'|' \
      "$APP/.env.example" > /etc/buggcraft-translation.env
  chmod 600 /etc/buggcraft-translation.env
fi
sed 's|/opt/buggcraft-translation/current|/opt/buggcraft-translation/app|g' \
    "$APP/deploy/buggcraft-translation.service" > /etc/systemd/system/buggcraft-translation.service
systemctl daemon-reload
systemctl enable --now buggcraft-translation.service
EOF
chmod 755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -eu
systemctl disable --now buggcraft-translation.service 2>/dev/null || true
EOF
chmod 755 "$STAGE/DEBIAN/prerm"

mkdir -p "$ROOT_DIR/dist"
dpkg-deb --build --root-owner-group "$STAGE" "$PACKAGE" >/dev/null
printf '%s\n' "$PACKAGE"
