# 大文件分片上传与失败重传（传输协议）

> 状态：设计定稿，待 agent 实现
> 适用范围：云盘（drive）+ 相册（photos）共享的**上传传输层**
> 依据：OPPO v4 分片协议探查（init→带CRC32块→part登记→complete；文件MD5秒传+块CRC32+断点+看门狗）
> 关联：`ARCHITECTURE.md` §6.5（drive API）、`SPEC-WEB-FRONTEND.md`（shared/uploader.ts）、
> `SPEC-05/06-DRIVE-MODULE`（transport）
> 目标：**大文件（数百 MB~数 GB）可靠上传**，分片 + 并发 + 断点续传 + 失败重传 + 秒传

## 1. 目标与非目标

- 目标：大文件可靠传云盘/相册；断网/崩溃/杀进程后续传；失败块自动重传；同文件秒传
- 非目标：实时进度之外的流控调优；服务端转码/后处理（另行）

## 2. 可靠性三层

| 层 | 手段 | 作用 |
|---|---|---|
| 文件级 | 整文件 MD5 | 判重秒传 + complete 终验 |
| 块级 | 单块 CRC32 | 传输正确性（单块失败只重传该块） |
| 会话级 | complete 整文件 MD5 + 看门狗 | 终态校验 + 卡住自动重传 |

## 3. 服务端接口（模块工厂生成，drive/photos 同构）

### 3.1 会话初始化（含秒传）

```
POST /api/{module}/upload/init
  req: { share?, path, filename, size, md5, chunkSize }
  resp:
    已存在（秒传）: { exists: true, fileId }
    新建会话:      { exists: false, uploadId, chunkSize }
                   { haveBlocks: [seq,...] }   # 断点：服务端已收块（续传依据）
```

### 3.2 单块上传

```
PUT /api/{module}/upload/{uploadId}/{seq}
  body: 单块二进制
  header: X-Chunk-CRC32: <hex>
  resp:
    成功: { ok: true, offset }
    失败: { code, msg }（客户端重传该块；CRC32 不符返回专属错码）
```

- 块大小固定 `chunkSize`；末块可小
- 落盘暂存 `{uploadId}/{seq}`，机械盘顺序写

### 3.3 完成

```
POST /api/{module}/upload/{uploadId}/complete
  req: { md5, size }
  resp: { fileId, size }   # 幂等：已 complete 直接返回成功
```

- 合并暂存块 → 整文件 MD5 校验 → 写入最终 `path`（自动建父目录）→ 清暂存
- **幂等**：并发/重试重复 complete 安全
- **磁盘满保护**：合并前查剩余空间，不足返回专属错码

### 3.4 批量判重（上传文件夹用）

```
POST /api/{module}/upload/batch-check
  req: { files: [{ path, md5, size }] }
  resp: { exists: { [md5]: fileId } }     # 已有跳过，其余逐个 init
```

### 3.5 会话清理

- 孤儿会话 TTL（如 24h 未 complete）→ 后台任务回收暂存
- complete 成功后即时清理

## 4. 客户端传输层（shared/uploader.ts · Android transport 统一）

```
uploadFile(file, target):
  1. 流式计算整文件 md5（增量，不整载内存）
  2. init → exists? 完成(秒传) : 得 uploadId + haveBlocks
  3. 块任务队列 [0..N]，跳过 haveBlocks
  4. 并发上传（默认 4 线程）
     每块: PUT {seq} + CRC32
       └─ 成功→标记；失败/超时→指数退避重试(2s/4s/8s…，≤5 次)
  5. 全部完成 → complete（整文件 md5 终验）
```

### 4.1 失败重传策略

| 场景 | 行为 |
|---|---|
| 单块 CRC32 不符/网络失败 | 仅重传该块，指数退避（2s/4s/8s…封顶 30s，≤5 次） |
| 单块重试超限 | 整任务暂停（非失败），保留会话 |
| 断网 / 杀进程 / App 崩溃 | 本地持久化会话 → 恢复后 init 拉 `haveBlocks` → 只传缺失 |
| 看门狗卡住 | 定期查总进度，连续 X 秒无进展且非暂停 → 取消当前块重传 |
| complete 网络失败 | 幂等重试（服务端已 complete 则直接成功） |

### 4.2 本地断点持久化

```
session = { uploadId, md5, size, chunkSize, haveBlocks:[...], target(path/share), updatedAt }
```
- Web：IndexedDB；Android：Room/文件
- 恢复流程：读本地 session → init（以服务端 `haveBlocks` 为准，覆盖本地）→ 只传缺失块 → complete
- 服务端 TTL 已清会话 → init 返回新 uploadId（重建会话，未传块重传）

### 4.3 并发与体验

- 块大小 `chunkSize` 默认 4MB（可配 1~16MB，按网络）；并发默认 4（可配）
- 传输面板：速度 / 进度 / 暂停 / 取消 / 重试（复用既有 TransferPanel）
- 上传文件夹：`batch-check` 批量秒传 → 逐个 init/上传

## 5. 状态机

```
[新文件] → init
            ├─ exists → [完成/秒传]
            └─ uploadId(+haveBlocks) → 上传缺失块（并发）
                                        ├─ 全成 → complete → [完成]
                                        └─ 块失败超限/断网 → 持久化会话 → [暂停]
暂停 → 恢复 → init 同步 haveBlocks → 传缺失 → complete
TTL 回收 → init 得新 uploadId（原暂存已清）→ 重建
```

## 6. 边界与安全

- uploadId 绑定设备 token + 目标 `path`，他端无法续传/覆盖
- complete 幂等；`batch-check` 只读判重
- 文件/块标识都带 CRC32/MD5，防中间损坏
- 磁盘满/TTL/孤儿清理由服务端兜底

## 7. 完成定义

- [x] init（判重秒传 + haveBlocks 断点）· 单块 PUT（CRC32）· complete（幂等 + 整文件 MD5）—— drive/photos 同构
- [x] 块落暂存 + 顺序合并 + 孤儿 TTL 清理（`upload.sweep` 每日）+ 磁盘满保护（专属错码）
- [x] 客户端并发 + 指数退避重试（≤5 次）—— Web uploader（shared/util.js chunkedUpload）
- [ ] 断点持久化（IndexedDB/Room）—— 客户端持久层；服务端 resume（init 带 uploadId + haveBlocks）已就绪
- [x] batch-check 批量秒传（drive/photos 均已提供服务端接口；Web 文件夹上传按文件 md5 秒传）
- [x] 看门狗/暂停/取消/传输面板—— 沿用既有 TransferPanel；看门狗为客户端优化项
