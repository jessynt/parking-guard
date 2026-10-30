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

## Helm / Kubernetes

Chart 位于 [`charts/parking-guard`](charts/parking-guard)，固定 `replicas: 1`，使用 `Recreate` 策略：升级时先停止旧 Pod，再启动新 Pod。程序只向外连接 MQTT 和 Bark，不创建 Service 或 Ingress。

先用项目的 Dockerfile 构建镜像，推送到集群可访问的仓库（将示例仓库地址换成自己的）：

```bash
docker build -t registry.example.com/parking-guard:0.1.0 .
docker push registry.example.com/parking-guard:0.1.0
cp charts/parking-guard/values.yaml parking-guard.values.yaml
```

编辑 `parking-guard.values.yaml`，至少修改以下字段。该文件已加入 `.gitignore`。
本地 `charts/parking-guard/values-production.yaml` 也已从 Git 和 Helm chart 包中排除；生产凭据保存在私有部署仓库中。

```yaml
image:
  repository: registry.example.com/parking-guard
  tag: "0.1.0"
config:
  PARK_LAT: "31.230416"
  PARK_LON: "121.473701"
  MQTT_HOST: "mosquitto.teslamate.svc.cluster.local"
secrets:
  BARK_KEY: "你的BarkKey"
  MQTT_USERNAME: ""
  MQTT_PASSWORD: ""
```

坐标和 MQTT 地址需替换为实际值；`BARK_KEY` 留空时只记日志。其余环境变量可在 `config` 中配置，凭据放在 `secrets` 中；`STATE_FILE` 固定为 `/data/parking_state.json`。私有镜像仓库可通过 `imagePullSecrets` 引用同 namespace 的拉取凭据。

检查并部署：

```bash
helm lint ./charts/parking-guard -f parking-guard.values.yaml
helm upgrade --install parking-guard ./charts/parking-guard \
  --namespace parking-guard --create-namespace \
  -f parking-guard.values.yaml --wait --timeout 5m
kubectl logs -n parking-guard deployment/parking-guard -f
```

默认创建 `1Gi` 的 `ReadWriteOnce` PVC 保存计时状态，使用集群默认 StorageClass。没有默认 StorageClass 时，设置 `persistence.storageClass`，或用 `persistence.existingClaim` 引用同 namespace 的现有 PVC。容器默认使用 UID/GID `10001`，PVC 需允许该用户组写入 `/data`。Chart 创建的 PVC 会在卸载时保留；不再需要状态时再手动删除。`persistence.enabled: false` 改用 `emptyDir`，Pod 重建会丢失计时状态。

也可以用 `secrets.existingSecret` 引用同 namespace 的现有 Secret，此时 chart 不创建 Secret，也不使用 `secrets` 下的凭据值。Secret 的键名使用 `BARK_KEY`、`MQTT_USERNAME`、`MQTT_PASSWORD`，未使用的键可省略。直接填写的凭据会保存在 Helm release 中；已有凭据管理流程时可使用现有 Secret。

修改 chart 管理的配置或凭据后，执行同一条 `helm upgrade --install` 命令即可触发重建。修改现有 Secret 的内容后，需要手动重启：

```bash
kubectl rollout restart deployment/parking-guard -n parking-guard
```

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
