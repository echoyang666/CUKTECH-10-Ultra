# 酷态科10号超级电能充Ultra（CUKTECH 10 GaN Charger Ultra）BLE 通信协议文档

> 本文档描述 **酷态科10号超级电能充Ultra（`AD1204U` / `njcuk.fitting.ad1204`）** 的本地低功耗蓝牙（BLE）通信协议规范，相关报文格式与属性编码已在真实硬件上完成端到端握手、加密通信与读写实测验证。

---

## 1. 目标设备与协议标识

| 项目 | 说明 |
| --- | --- |
| 产品名称 | 酷态科10号超级电能充Ultra 充电器（`AD1204U`） |
| MIoT Model | `njcuk.fitting.ad1204`（兼容 `njcuk.fitting.1204e`） |
| MiBeacon PDID | `26126`（`0x660E`） |
| BLE GATT 服务 | `0xFE95`（`0000FE95-0000-1000-8000-00805F9B34FB`） |
| 会话认证 | MiBLE Standard Auth（HKDF-SHA256 + HMAC-SHA256 + AES-128-CCM） |
| 应用层协议 | MIoT-BLE Spec v2 |

---

## 2. 总体通信架构

该充电器为**纯低功耗蓝牙（BLE-only）设备**，无 Wi-Fi 模块。客户端通过 **BLE GATT 服务 `0xFE95`** 与设备直连，协议自底向上分为 4 层：

```text
+-------------------------------------------------------------------+
| Layer 4: 酷态科 10 Ultra 业务模型 (MIoT Spec siid=1,2 属性/动作)    |
+-------------------------------------------------------------------+
| Layer 3: MIoT-BLE Spec v2 二进制帧 (version|len, tid, opcode, TLV) |
+-------------------------------------------------------------------+
| Layer 2: MiBLE Standard Auth (auth_type=4) 会话层 AES-128-CCM      |
+-------------------------------------------------------------------+
| Layer 1: MiBLE Parcel 分包/重组协议 (CMD 000000 / 000002 / ACK)    |
+-------------------------------------------------------------------+
| Layer 0: BLE GATT Service 0xFE95 (0004 / 0010 / 0019 / 001A / 001B)|
+-------------------------------------------------------------------+
```

> **说明**：MIoT-BLE Spec 业务读写与通知均承载在 `0xFE95` 服务的 `001A`（请求/响应）与 `001B`（主动上报）特征值上。

---

## 3. Layer 0 — BLE GATT 服务与特征值

所有特征值均位于 GATT 服务 **`0000FE95-0000-1000-8000-00805F9B34FB` (`0xFE95`)** 下：

| 简写 UUID | 完整 UUID | 用途 | 方向 / 属性 |
| --- | --- | --- | --- |
| `0004` | `00000004-0000-1000-8000-00805F9B34FB` | 固件版本（UTF-8） | Read |
| `0010` | `00000010-0000-1000-8000-00805F9B34FB` | StandardAuth 控制通道 | Write + Notify |
| `0019` | `00000019-0000-1000-8000-00805F9B34FB` | StandardAuth 数据分包通道 | WriteWithoutResponse + Notify |
| `001A` | `0000001A-0000-1000-8000-00805F9B34FB` | **MIoT-BLE Spec 请求与响应通道** | WriteWithoutResponse + Notify |
| `001B` | `0000001B-0000-1000-8000-00805F9B34FB` | **MIoT-BLE Spec 主动上报通道**（属性变更/事件） | WriteWithoutResponse + Notify |

### 蓝牙广播识别（MiBeacon `0xFE95` Service Data）

设备广播 `0xFE95` Service Data：
- Bytes `0..1`（uint16 LE）：Frame Control
- Bytes `2..3`（uint16 LE）：Product ID（`26126` / `0x660E`）
- Byte `4`：Frame Counter
- 若 Frame Control bit 4 置位，Bytes `5..10` 为反序 6 字节设备 MAC 地址。

---

## 4. Layer 1 — MiBLE Parcel 分包与重组协议

`0019`、`001A`、`001B` 三条通道均使用相同的分包协议，默认每包净荷 `DMTU = 18` 字节（加上 2 字节包序号共 20 字节，兼容最小 BLE ATT MTU = 23）。

### 4.1 控制帧（前 2 字节固定为 `00 00`）

| 帧类型 | 十六进制格式 | 含义 |
| --- | --- | --- |
| 多包发送请求（CMD） | `00 00 00 <data_type:1B> <total_frames:u16_LE>` | 请求发送 `total_frames` 个分包 |
| 单包发送（SinglePack） | `00 00 02 <data_type:1B> <payload>` | 短报文单包直传 |
| 多包 ACK — 准备就绪 | `00 00 01 01` | 接收端就绪，发送端开始发送分包 `1..N` |
| 多包 ACK — 接收成功 | `00 00 01 00` | 接收端已完整收到所有分包 |
| 多包 ACK — 设备忙 | `00 00 01 02` | 发送失败：busy |
| 多包 ACK — 超时 | `00 00 01 03` | 发送失败：timeout |
| 多包 ACK — 取消 | `00 00 01 04` | 发送失败：cancel |
| 多包 ACK — 请求重传 | `00 00 01 05 <seq:u16_LE>` | 从分包序号 `seq` 开始重传 |
| 单包 ACK — 成功 | `00 00 03 00` | 单包接收成功 |
| 单包 ACK — 设备忙 | `00 00 03 02` | 单包接收失败：busy |

### 4.2 数据帧

- 格式：`<seq:u16_LE> <chunk_bytes>`，其中 `seq` 从 `1` 递增到 `total_frames`（`seq >= 1` 区分于 `00 00` 控制帧）。
- `data_type` 取值：
  - `0019` StandardAuth：`0x0B`（`app_rand`）、`0x0D`（`dev_rand`）、`0x0C`（`dev_confirm`）、`0x0A`（`app_confirm`）
  - `001A` / `001B` MIoT-BLE Spec：`0x00`

---

## 5. Layer 2 — Standard Auth 登录与 AES-128-CCM 会话加密

设备采用 **MiBLE Standard Auth（`auth_type = 4`）** 双向认证与会话加密。

### 5.1 登录握手流程

前提条件：客户端持有该设备的 **12 字节（24 位十六进制）`token`**（注意**不是** 16 字节广播加密用的 `beaconkey`）。

1. 订阅 `0019` 与 `0010` 的 BLE Notify。
2. （可选）读取 `0004` 获取固件版本。
3. 向 `0010` 写入登录起始指令：`24 00 00 00`。
4. 生成 16 字节随机 `app_rand`，通过 `0019` 分包发送（`data_type = 0x0B`）。
5. 从 `0019` 依次接收：
   - `dev_rand`（16 字节，`data_type = 0x0D`）
   - `dev_confirm`（32 字节，`data_type = 0x0C`）
   *(注：客户端按 `data_type` 区分，不依赖到达顺序。)*
6. 使用 **HKDF-SHA256**（RFC 5869）派生 64 字节会话密钥材料：
   - `IKM` = `token`（12 字节原始字节）
   - `salt` = `app_rand || dev_rand`（32 字节）
   - `info` = `b"mible-login-info"`（UTF-8）
   - `L` = `64` 字节
   - 切分：
     - `dev_key = okm[0:16]`（16B，设备 -> 客户端解密密钥）
     - `app_key = okm[16:32]`（16B，客户端 -> 设备加密密钥）
     - `dev_iv  = okm[32:36]`（4B，设备 -> 客户端 Nonce 前缀）
     - `app_iv  = okm[36:40]`（4B，客户端 -> 设备 Nonce 前缀）
     - `reserved = okm[40:64]`（24B）
7. 双向认证校验：
   - 计算 `expected_dev_confirm = HMAC-SHA256(key=dev_key, msg=dev_rand || app_rand)`
   - 验证 `expected_dev_confirm == dev_confirm`
   - 计算 `app_confirm = HMAC-SHA256(key=app_key, msg=app_rand || dev_rand)`
8. 通过 `0019` 分包发送 `app_confirm`（32 字节，`data_type = 0x0A`）。
9. 从 `0010` 收到登录结果通知：`21 00 00 00`（首字节 `0x21` 表示登录成功，`0x23` 表示认证失败）。

### 5.2 会话加密（AES-128-CCM）

- **加密（客户端 -> 设备，写入 `001A`）**：
  - 维护单调递增 32 位计数器 `app_counter`（每次加密后 `+1`）。
  - `nonce`（12 字节）= `app_iv(4B) || 00 00 00 00(4B) || uint32_LE(app_counter)(4B)`
  - `aad` = 空字节 `b""`
  - `tag_len` = `4` 字节
  - `ct_with_tag = AES-128-CCM-Encrypt(key=app_key, nonce=nonce, plaintext=spec_frame, aad=b"", tag_len=4)`
  - 线上传输密文 = `uint16_LE(app_counter & 0xFFFF)(2B) || ct_with_tag`
- **解密（设备 -> 客户端，来自 `001A` 或 `001B`）**：
  - 取前 2 字节 `counter_lo2 = data[0:2]`，密文+TAG 为 `data[2:]`。
  - `nonce`（12 字节）= `dev_iv(4B) || 00 00 00 00(4B) || counter_lo2(2B) || 00 00(2B)`
  - `plaintext = AES-128-CCM-Decrypt(key=dev_key, nonce=nonce, ciphertext=data[2:], aad=b"", tag_len=4)`

---

## 6. Layer 3 — MIoT-BLE Spec v2 二进制帧格式

### 6.1 公共帧头（5 字节）

```text
Byte 0..1 (uint16 LE): (version << 12) | total_frame_length
Byte 2..3 (uint16 LE): tid (递增事务 ID)
Byte 4    (uint8)    : opcode
```

### 6.2 Opcode 表

| Opcode | 名称 | 方向 | 负载格式（从 Byte 5 起） |
| --- | --- | --- | --- |
| `0x00` | `set_properties` | App -> Dev | `count(1B)` + `count` × `[siid(1B), piid(2B LE), type_len(2B LE), value(len)]` |
| `0x01` | `set_properties-rsp` | Dev -> App | `count(1B)` + `count` × `[siid(1B), piid(2B LE), code(2B int16 LE)]` |
| `0x02` | `get_properties` | App -> Dev | `count(1B)` + `count` × `[siid(1B), piid(2B LE)]` |
| `0x03` | `get_properties-rsp` | Dev -> App | `count(1B)` + `count` × `[siid(1B), piid(2B LE), code(2B int16 LE), (若 code==0: type_len(2B LE), value(len))]` |
| `0x04` | `properties_changed` | Dev -> App (`001B`) | `count(1B)` + `count` × `[siid(1B), piid(2B LE), type_len(2B LE), value(len)]` |
| `0x05` | `action` | App -> Dev | `siid(1B), aiid(1B), count(1B)` + `count` × `[piid(2B LE), type_len(2B LE), value(len)]` |
| `0x06` | `action-rsp` | Dev -> App | `code(2B int16 LE)` + `(若 code==0: count(1B) + count × [piid(2B LE), type_len(2B LE), value(len)])` |
| `0x07` | `event_occured` | Dev -> App (`001B`) | `siid(1B), eiid(1B), count(1B)` + `count` × `[piid(2B LE), type_len(2B LE), value(len)]` |
| `0xF0` | `protocol_exchange` | 双向 | 请求 5 字节无负载（`version=2`）；响应含设备支持的协议版本号 |

### 6.3 `type_len` 与数据类型编码

`type_len`（uint16 LE）= `(value_type << 12) | byte_length`，标量值均为**小端序（Little-Endian）**：

| `value_type` | 类型名 | 字节长度 | 说明 |
| --- | --- | --- | --- |
| `0` | `bool` | 1 | `0x00` / `0x01` |
| `1` | `uint8` | 1 | 无符号 8 位整数 |
| `2` | `int8` | 1 | 有符号 8 位整数 |
| `3` | `uint16` | 2 | 无符号 16 位整数 LE |
| `4` | `int16` | 2 | 有符号 16 位整数 LE |
| `5` | `uint32` | 4 | 无符号 32 位整数 LE |
| `6` | `int32` | 4 | 有符号 32 位整数 LE |
| `7` | `uint64` | 8 | 无符号 64 位整数 LE |
| `8` | `int64` | 8 | 有符号 64 位整数 LE |
| `9` | `float` | 4 | IEEE 754 float32 LE |
| `10` | `string` | `len(utf8)` | UTF-8 字符串 |
| `15` (`0x0F`) | `raw/null` | `byte_length` | 实机观测到的保留/空类型，按长度跳过并继续解析同帧剩余项 |

---

## 7. Layer 4 — 酷态科10号超级电能充Ultra 属性与动作定义

### 7.1 Service 1 — `device_information`

| siid.piid | 名称 | 类型 | 权限 | 说明 |
| --- | --- | --- | --- | --- |
| `1.1` | `manufacturer` | `string` (`10`) | R | 制造商 |
| `1.2` | `model` | `string` (`10`) | R | 型号 (`njcuk.fitting.ad1204`) |
| `1.3` | `serial_number` | `string` (`10`) | R | 序列号 |
| `1.4` | `firmware_revision` | `string` (`10`) | R | 固件版本（`>= 2.1.2_0040` 支持 Spec v2 扩展项） |

### 7.2 Service 2 — `charger` 属性表

| siid.piid | 名称 | 类型 (`type`) | 权限 | 编码格式与取值 |
| --- | --- | --- | --- | --- |
| `2.1` | `port_info_c1` | `uint32` (`5`) | R/N | C1 端口实时遥测（见 7.3） |
| `2.2` | `port_info_c2` | `uint32` (`5`) | R/N | C2 端口实时遥测（见 7.3） |
| `2.3` | `port_info_c3` | `uint32` (`5`) | R/N | C3 端口实时遥测（`status=0x11` 表示 C3&A 共享 5V） |
| `2.4` | `port_info_a` | `uint32` (`5`) | R/N | USB-A 端口实时遥测（见 7.3） |
| `2.5` | `scene_mode` | `uint8` (`1`) | R/W/N | 功率分配模式：`1`=AI智能，`2`=数码生态，`3`=极速单充，`4`=均衡输出 |
| `2.6` | `screen_save_time` | `uint8` (`1`) | R/W/N | 熄屏时间：`5`=1分钟，`1`=5分钟，`2`=10分钟，`3`=30分钟，`4`=常亮 |
| `2.7` | `protocol_ctrl` | `uint8` (`1`) | R/W/N | 协议开关（Spec v1 兼容掩码）：`bit0`=SCP，`bit1`=MiPPS，`bit2`=UFCS |
| `2.8` | `delay_down_setting` | `uint8` (`1`) | R/W/N | 端口定时关闭使能掩码：`bit0`=C1，`bit1`=C2，`bit2`=C3，`bit3`=A |
| `2.9` | `delay_time_c1` | `uint16` (`3`) | R/W/N | C1 定时关闭时长（分钟，`0`=关闭，需与 `2.8` 一同写入） |
| `2.10` | `delay_time_c2` | `uint16` (`3`) | R/W/N | C2 定时关闭时长（分钟） |
| `2.11` | `delay_time_c3` | `uint16` (`3`) | R/W/N | C3 定时关闭时长（分钟） |
| `2.12` | `delay_time_a` | `uint16` (`3`) | R/W/N | USB-A 定时关闭时长（分钟） |
| `2.13` | `screen_language` | `uint8` (`1`) | R/W/N | 屏幕语言：`0`=英文，`1`=中文 |
| `2.14` | `enter` | `uint8` (`1`) | W | 前台进入通知：连接后写入 `2` 触发设备开始推送实时功率数据 |
| `2.15` | `usb_a_always_on` | `bool` (`0`) | R/W/N | USB-A 小电流模式（开启后 4 小时自动关闭） |
| `2.16` | `port_ctrl` | `uint8` (`1`) | R/W/N | 端口输出开关位掩码：`bit0`=C1，`bit1`=C2，`bit2`=C3，`bit3`=A（`1`=开，`0`=关） |
| `2.17` | `c1_c2_protocol` | `uint32` (`5`) | R/N | C1/C2 握手快充协议与档位功率（见 7.4） |
| `2.18` | `c3_a_protocol` | `uint32` (`5`) | R/N | C3/A 握手快充协议与档位功率（见 7.4） |
| `2.19` | `screenoff_while_idle` | `uint8` (`1`) | R/W/N | 空闲 5 分钟自动熄屏（`0`/`1`，Spec v2） |
| `2.20` | `screen_direction_lock` | `uint8` (`1`) | R/W/N | 屏幕方向锁定（`0`/`1`，Spec v2） |
| `2.21` | `protocol_ctl_extend` | `uint32` (`5`) | R/W/N | 分端口快充协议扩展开关（见 7.5，Spec v2） |

### 7.3 端口实时遥测位域 (`2.1`–`2.4`, `uint32`)

```text
Bit 31..24: voltage_raw  -> 电压 = voltage_raw / 10.0  (V)
Bit 23..16: current_raw  -> 电流 = current_raw / 10.0  (A)
Bit 15..8 : protocol_raw -> 协议/使能状态（0 表示端口未使能或已关闭）
Bit  7..0 : status       -> 0 = 无输出；1 = 输出中；0x11 (C3) = C3 & A 共享 5V 合并显示
计算功率  : power_w = round(voltage * current, 1)
```

### 7.4 端口协议与功率 (`2.17` & `2.18`, `uint32`)

- `2.17` (`c1_c2_protocol`)：`Byte3 = C1 协议ID`，`Byte2 = C1 协议功率(W)`，`Byte1 = C2 协议ID`，`Byte0 = C2 协议功率(W)`
- `2.18` (`c3_a_protocol`)：`Byte3 = C3 协议ID`，`Byte2 = C3 协议功率(W)`，`Byte1 = A 协议ID`，`Byte0 = A 协议功率(W)`
- 协议 ID 枚举：
  - `1`, `2` -> `5V`
  - `3` -> `QC`
  - `4` -> `AFC`
  - `5` -> `FCP`
  - `6` -> `SCP`
  - `7` -> `PD`
  - `8`, `9` -> `PPS`
  - `10` -> `UFCS`

### 7.5 分端口协议扩展开关 (`2.21`, `uint32`)

```text
Byte 0 (Bits  0..7 , C1): 0x08 | (ufcs << 2) | (pps << 1) | (pd << 0)
Byte 1 (Bits  8..15, C2): 0x08 | (ufcs << 2) | (pps << 1) | (pd << 0)
Byte 2 (Bits 16..23, C3): (scp << 1) | (ufcs << 0)
Byte 3 (Bits 24..31,  A): (scp << 1) | (ufcs << 0)
```

### 7.6 Service 2 — 动作 (`Action`)

| siid.aiid | 名称 | 入参 | 说明 |
| --- | --- | --- | --- |
| `2.1` | `reset_setting` | `[]` | 恢复充电器默认设置 |

---

## 8. 实机联调与平台实现要点

以下为在 macOS + 真实 `njcuk.fitting.ad1204` 硬件上验证总结的工程要点（均已集成至 `cuktech_ultra`）：

1. **`token` 与 `beaconkey` 区分**：StandardAuth 登录使用 12 字节 `token`（24 位 hex），而非 16 字节广播解密用的 `beaconkey`。
2. **macOS `uvicorn` 事件循环**：启动 FastAPI 服务时必须指定 `loop="asyncio"`，避免 `uvloop` 导致 `bleak` CoreBluetooth 回调收不到扫描事件。
3. **macOS 系统已连接设备召回**：当设备已由系统层连接（停止广播）时，通过 `BleakAdapter.get().get_connected_devices([0xFE95])`（底层 `retrieveConnectedPeripheralsWithServices:`）可直接找回外设句柄。
4. **GATT 写入模式自适应**：写入特征值前自动检查 `characteristic.properties`（`write` vs `write-without-response`），防止 CoreBluetooth 因写模式不匹配拒绝写入。
5. **非标 `value_type = 15` 容错**：实机响应中存在 `type_id = 15` (`0x0F`) 字段，按长度步进跳过即可完整解析同帧后续属性。
