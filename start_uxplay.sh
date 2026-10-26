#!/bin/sh
# AirPlay receiver for the iPhone screen. Dumps frames as JPEGs into /dev/shm/iphone for the
# web console and the agent (the leaky queue drops frames while jpegenc is busy), and shows
# the phone full-screen on the Pi's own display when a desktop session is running.
# The mirror is requested at 2x IPHONE_POINTS. /dev/shm/iphone/state says whether an iPhone
# is mirroring. Restarts UxPlay if it exits.
#
# The decoder runs single-threaded: frame threading holds frames back, and the iPhone only
# sends frames when the screen changes, so the newest frame would be stuck in the decoder.
DIR=$(cd "$(dirname "$0")" && pwd)
# Read single values (config.env has values with spaces, so it is not sourced).
conf() { sed -n "s/^$1=//p" "$DIR/config.env" 2>/dev/null | tail -1 | tr -d "\"'"; }
MIRROR_NAME=$(conf MIRROR_NAME)
IPHONE_POINTS=$(conf IPHONE_POINTS)
: "${MIRROR_NAME:=Pi-Agent}" "${IPHONE_POINTS:=430x932}"
W=$(( ${IPHONE_POINTS%x*} * 2 ))
H=$(( ${IPHONE_POINTS#*x} * 2 ))

FRAMES=/dev/shm/iphone
mkdir -p "$FRAMES"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

while true; do
  rm -f "$FRAMES"/frame-*.jpg
  echo idle > "$FRAMES/state"
  if [ -S "$XDG_RUNTIME_DIR/wayland-0" ]; then
    export WAYLAND_DISPLAY=wayland-0
    SINK="-fs -vs waylandsink"
  else
    SINK="-vs fakesink"  # no desktop: frames for the web console only
  fi
  echo "starting UxPlay as '$MIRROR_NAME', ${W}x${H}, sink: $SINK"
  # shellcheck disable=SC2086
  stdbuf -oL -eL uxplay -n "$MIRROR_NAME" -nh -s "${W}x${H}" $SINK \
    -vd "avdec_h264 max-threads=1" \
    -vc "videoconvert ! tee name=t t. ! queue leaky=downstream max-size-buffers=1 ! jpegenc quality=85 ! multifilesink location=$FRAMES/frame-%05d.jpg max-files=4 t. ! queue ! videoconvert" \
    2>&1 | while IFS= read -r line; do
      echo "$line"
      case "$line" in
        *"Begin streaming"*) echo mirroring > "$FRAMES/state" ;;
        *"raop_rtp_mirror->running is no longer true"*)
          echo idle > "$FRAMES/state"
          rm -f "$FRAMES"/frame-*.jpg ;;
      esac
    done
  echo "uxplay exited, restarting in 2 s"
  sleep 2
done
