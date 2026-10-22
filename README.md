# 9654 x86 Pause/Resume 自动化测试包

本包使用社区 CubeSandbox 源码的 one-click 构建与安装入口，在单机上建立模板，再按 c1/c5/c10、各 1 轮预热和 5 轮正式测量 Pause/Resume。所有结果先标为 `pending`，不会自动认证成基线或优化收益。测试前清理只针对本包记录的沙箱；若发现其他库存，脚本停止。

## 迁移与配置

将本目录和 CubeSandbox 源码一起复制到 9654 x86 主机。要求物理机或可用 `/dev/kvm` 的 Linux、root、Docker、Docker Compose、Git、Python 3.9+、构建所需网络与足够磁盘。one-click 构建会编译 CubeMaster、Cubelet、CubeAPI、Shim、runtime、agent、guest image 等；安装会改动 systemd、Docker、DNS 和 `/usr/local/services/cubetoolbox`，应在专用测试机操作。

部署、模板创建和性能测试命令请在 root 会话执行。若源码还在其它分支，先在源码目录执行 `git fetch --tags` 和 `git checkout v0.7.1`，确认没有未提交改动。

```bash
cd /home/zz/9654
cp config.example.json config.json
# 编辑 source_dir，并将源码检出到 v0.7.1；默认 expected_git_ref 会核对版本。
# node_ip 建议写 9654 主机实际内网 IP。
python3 suite.py status
```

`source_dir` 可指向迁移后的任意源码路径。`kernel_file` 可填已有的 x86 guest `vmlinux`；留空时构建阶段从 `kernel_url` 下载。若需要固定制品，在 `kernel_sha256` 填入事先核对的哈希。源码工作区必须干净；脚本记录 commit 与制品 SHA256。`template_id` 留空时使用 `template_alias` 创建模板。若已有 READY 模板，可以填写其 ID 并跳过模板创建。

默认要求源码为 `v0.7.1`，与之前那台机器的原版对照版本一致。9654 是另一台硬件，结果需作为独立基线；不能直接把两台机器的毫秒值相减作为代码收益。建议把 `template_image` 改为固定 digest，避免 `latest` 镜像漂移。

## 分阶段执行

构建只生成本地发布包和测试用 Python 虚拟环境，不安装或启动实验。之后每个会改变机器或启动沙箱的阶段都要先看本次只读预检，再由操作者手动创建一次性确认记录。

```bash
python3 suite.py build

python3 suite.py preflight deploy
python3 suite.py approve preflight/<上条输出的文件名> --phrase '确认本次部署'
python3 suite.py run deploy --approval approvals/<上条输出的文件名>

python3 suite.py preflight template
python3 suite.py approve preflight/<上条输出的文件名> --phrase '确认本次模板创建'
python3 suite.py run template --approval approvals/<上条输出的文件名>

python3 suite.py preflight bench
python3 suite.py approve preflight/<上条输出的文件名> --phrase '确认本次性能测试'
python3 suite.py run bench --approval approvals/<上条输出的文件名>
```

构建阶段会在本目录的 `.venv` 安装源码内的 `cubesandbox` SDK。若 `node_ip` 已配置，SDK 会直连节点的 CubeProxy HTTP 端口，绕过 `cube.app` DNS。

`preflight` 展示负载、CPU/NUMA、内存与 PSI、磁盘、libvirt VM、库存、服务/节点健康、二进制哈希、模板、THP/NUMA balancing、源码身份及本次计划。存在阻断项时不能创建有效确认记录。`approve` 只是把操作者对**这一份**预检的明确确认写入一次性记录；`run` 会重新检查条件，条件变化或记录已使用则停止。不要在自动化命令里串联 `preflight → approve → run`。

部署后若安装器失败，脚本保留日志和状态，不自动再次安装。模板创建异步等待 READY，超时停止；不会自动重建模板。测试每轮前要求 load1 连续 `quiet_seconds` 秒小于 `load1_max`，最多等待 `quiet_timeout_seconds`；无采样、无重试、无异常值剔除。测试会先清理上次由本包记录的残留沙箱，随后做一个文件与内存状态功能冒烟，最后运行正式轮次。任一步失败即停止，结果仍为 `pending`。

结果和日志在 `runs/`，每轮原值、环境快照、二进制哈希及故障记录均保存。测试结束后检查 `run.json` 和 `rounds.json`，由用户另行认证后再写正式结论。需要重新测试时必须重新预检和确认。

## 限制

- one-click 部署适用于社区源码对应的单机包；该包需自行联网获取 guest kernel、构建依赖和容器镜像。下载失败时检查 `kernel_url` 或改用本地 `kernel_file`。
- 本包不删除其他用户沙箱、模板、Docker 卷或系统数据，也不会停止其他人的虚拟机。
- 9654 上的实际资源、网络和模板镜像尚未验证；第一次预检会列出缺失项。当前主机未运行此包的部署或性能测试。
