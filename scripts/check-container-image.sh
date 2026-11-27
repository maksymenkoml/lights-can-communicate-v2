#!/bin/sh
# Start the core image on an empty data folder with two private Docker networks
# (no internet, no LAN, no lamps) and check that Light Studio and the virtual
# bridge answer, that the first start wrote the data folder, and that the
# container stops cleanly. Usage: sh scripts/check-container-image.sh <image>
set -eu
image=$1
name=lcc-image-check
data=$(mktemp -d)
cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker network rm lcc-check-core lcc-check-lan >/dev/null 2>&1 || true
    docker run --rm -v "$data:/data" --entrypoint sh "$image" -c 'rm -rf /data/* /data/.[!.]*' >/dev/null 2>&1 || true
    rmdir "$data" 2>/dev/null || true
}
trap cleanup EXIT
docker network create --internal --subnet 172.30.250.0/29 --gateway 172.30.250.1 lcc-check-core >/dev/null
docker network create --internal --subnet 10.250.0.0/24 --gateway 10.250.0.1 lcc-check-lan >/dev/null
docker create --name "$name" --network lcc-check-core --ip 172.30.250.3 \
    -e CORE_ADDRESS=10.250.0.40 -e TZ=Europe/Kyiv -e STUDIO_HOSTS=nas.local \
    -v "$data:/data" --stop-timeout 20 "$image" >/dev/null
docker network connect --ip 10.250.0.40 lcc-check-lan "$name"
docker start "$name" >/dev/null

probe() {
    # Ask as a LAN client would, from a sibling container on the LAN network.
    docker run --rm --network lcc-check-lan --entrypoint python3 "$image" -I -c "
import sys, urllib.request
request = urllib.request.Request(sys.argv[1], headers={'Host': sys.argv[2]})
print(urllib.request.urlopen(request, timeout=3).read(4096).decode())" "$@"
}
ok=""
i=0
while [ $i -lt 60 ]; do
    if studio=$(probe http://10.250.0.40:19881/ 10.250.0.40:19881 2>/dev/null) &&
       bridge=$(probe http://10.250.0.40/api/config 10.250.0.40 2>/dev/null); then
        ok=1
        break
    fi
    i=$((i + 1))
    sleep 2
done
if [ -z "$ok" ]; then
    docker logs "$name" 2>&1 | tail -60
    echo "The core did not answer" >&2
    exit 1
fi
printf '%s' "$studio" | grep -q "Light Studio"
printf '%s' "$bridge" | grep -q '"bridgeid"'
docker exec "$name" sh -c 'test -s /data/core.json && test -s /data/state/adapter-token && test -s /data/state/power-owner-token && test -s /data/version && test -d /data/state'
docker exec "$name" sh -c 'test "$(stat -c %a /data/state/adapter-token)" = 600'
start=$(date +%s)
docker stop "$name" >/dev/null
code=$(docker inspect -f '{{.State.ExitCode}}' "$name")
elapsed=$(( $(date +%s) - start ))
if [ "$code" != 0 ] || [ "$elapsed" -ge 20 ]; then
    docker logs "$name" 2>&1 | tail -40
    echo "The core did not stop cleanly (exit $code after ${elapsed}s)" >&2
    exit 1
fi
# A second start keeps the identity.
before=$(docker run --rm -v "$data:/data" --entrypoint python3 "$image" -I -c "import json;print(json.load(open('/data/core.json'))['gateway']['mac'])")
docker start "$name" >/dev/null
sleep 5
docker stop "$name" >/dev/null
after=$(docker run --rm -v "$data:/data" --entrypoint python3 "$image" -I -c "import json;print(json.load(open('/data/core.json'))['gateway']['mac'])")
[ "$before" = "$after" ]
echo "Container image check passed"
