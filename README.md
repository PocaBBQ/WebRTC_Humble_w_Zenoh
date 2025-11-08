# WebRTC_Humble_w_Zenoh
Learning WebRTC with ROS2 Humble, combine with Zenoh

<img width="727" height="571" alt="Screenshot from 2025-11-08 20-02-38" src="https://github.com/user-attachments/assets/cad81181-7418-44cf-9b76-39aea8bb24db" />

<img width="440" height="467" alt="Screenshot from 2025-11-08 20-03-29" src="https://github.com/user-attachments/assets/5c08f79f-3498-4b4f-88e7-424bd6114223" />


---

## ✨ Features

- ✅ **ROS 2 Humble node** — `ros_streamer_server`
- 📡 **WebRTC (aiortc)** — browser-native low-latency video
- 🧠 **Zenoh signaling** — pub/sub discovery without a central server
- 🔄 **MediaRelay** — one camera → many viewers (no “Device busy”)
- 🌐 **FastAPI WebSocket bridge** — browser ↔ ROS 2 interop
- 🖥️ **HTTP test UI** — select resolution/FPS and view the stream

---

## ⚙️ Installation

### System packages
```bash
sudo apt update
sudo apt install \
  ros-humble-ros-base python3-colcon-common-extensions \
  python3-opencv v4l-utils python3-pip

pip install aiohttp fastapi uvicorn aiortc zenoh[python] websockets

cd ~/TTT/WebRTC_Humble
colcon build --symlink-install
source install/setup.bash

zenohd
python3 ws_zenoh_bridge/bridge.py
ros2 launch ros_streamer ros_streamer.launch.py room:=cam1
