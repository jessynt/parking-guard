# Parking Guard

从 TeslaMate 的 MQTT 数据读取车辆位置，在停车优惠结束前通过 Bark 提醒。

```text
Tesla → TeslaMate → MQTT → parking-guard → Bark
```

车辆进入停车场 80 米内并挂 P/N 挡，连续 90 秒后开始计时。优惠剩余 30、10、5 分钟时推送，最后一档为 `critical`；到点后不再推送。

离开 150 米后结束计时并推送本次停留时长。计时写入状态文件，重启后继续。

## 运行

```bash
python3 -m venv .env
source .env/bin/activate
pip install -r requirements.txt
cp parking-guard.env.example parking-guard.env
# 编辑 parking-guard.env，至少填写 PARK_LAT、PARK_LON、MQTT_HOST、BARK_KEY
python main.py
```

Windows 激活命令：`.env\Scripts\Activate.ps1`。

模拟和测试：

```bash
python simulate.py
python -m unittest discover -s tests
```

## Docker

把本目录放在 TeslaMate 的 `docker-compose.yml` 同级目录，然后：

```bash
cp parking-guard/parking-guard.env.example parking-guard/parking-guard.env
# 编辑 parking-guard.env，设置 MQTT_HOST
docker compose -f docker-compose.yml -f parking-guard/docker-compose.parking-guard.yml up -d --build
docker logs -f parking-guard
```

`MQTT_HOST` / `MQTT_PORT` 由 `parking-guard.env` 指定，不依赖 compose 里的 `mosquitto`。改代码后需要重新 `--build`，状态保存在 `parking-guard/data`。

## 配置

完整配置见 `parking-guard.env.example`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PARK_LAT` / `PARK_LON` | 必填 | 停车场坐标，WGS-84 |
| `DISCOUNT_MINUTES` | `180` | 优惠时长（分钟） |
| `WARN_MINUTES` | `30,10,5` | 提醒档位，最后一档为 critical |
| `PARK_RADIUS_ENTER` | `80` | 进入半径（米） |
| `PARK_RADIUS_EXIT` | `150` | 离开半径（米，需 ≥ 进入半径） |
| `PARK_CONFIRM_SECONDS` | `90` | 入场确认时间（秒） |
| `MQTT_HOST` | 必填 | broker 地址 |
| `MQTT_PORT` | `1883` | broker 端口 |
| `CAR_ID` | `1` | TeslaMate 车辆编号 |
| `BARK_KEY` | — | Bark device key，留空只记日志 |

坐标使用 WGS-84，高德、腾讯地图是 GCJ-02，会有偏移。

## 排查

- 一直显示“场外”：检查坐标、半径和挡位。
- 显示“仍未收到经纬度”：检查 `CAR_ID`、`MQTT_PREFIX` 和 TeslaMate 的 MQTT 发布。
- 需要更多日志：设置 `LOG_LEVEL=DEBUG` 后重建容器。
