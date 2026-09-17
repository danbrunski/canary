# runs ON the Pi: kernel counter read immediately before and after each daemon /status read
T=$(cat /etc/canary/control.token)
K() { echo "$(date +%s.%N) $(cat /sys/class/net/can0/statistics/rx_packets) $(cat /sys/class/net/can0/statistics/rx_over_errors) $(cat /sys/class/net/can0/statistics/rx_dropped)"; }
D() { curl -s -m 8 -H "Authorization: Bearer $T" http://127.0.0.1:8000/status | python3 -c 'import sys,json; s=json.load(sys.stdin); r=s["health"]["obd_scheduler"]["receive"]; v=s["vehicle_state"]; print(r["frames_seen"], v["state"], v["rpm"])'; }
echo "start_pre  $(K)"; echo "start_d    $(D)"; echo "start_post $(K)"
sleep ${1:-300}
echo "end_pre    $(K)"; echo "end_d      $(D)"; echo "end_post   $(K)"
