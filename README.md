# Breeze-TTS-2 · Ubuntu CUDA 训练

`ubuntu` 分支运行 CUDA LoRA 训练。Mac 工作台、素材审核、试听、发布和聊天在 [`main` 分支](https://github.com/zchengyu854/Sound/tree/main)。

## 运行

在 Ubuntu 安装支持 CUDA 13 的 NVIDIA 驱动和 `uv`。把已经导出的冻结任务 ZIP 传到 Ubuntu 后执行：

```sh
uv run --python 3.12 --with-requirements requirements-ubuntu.txt \
  python ubuntu_training.py /path/to/task.zip /path/to/result.zip
```

使用已缓存的同一基座时可追加 `--base-dir /path/to/base`。终端显示步数、损失和 MLX 峰值显存。用同一个任务 ZIP 和输出目录再次运行会恢复最近一次完整检查点。不同任务使用不同的结果文件名。

训练结束后，把结果 ZIP 传回 Mac，在原任务中选择「导入 Ubuntu 结果包」。Mac 校验快照、配置和基座权重，再生成 A/B 试听。取消远端训练使用 Ubuntu 终端的 Ctrl+C。

## 训练监控页面

训练和监控分开：CLI 负责训练，页面只读取任务目录，不会启动或停止训练。建议每个任务使用独立目录：

```sh
mkdir -p training-runs/demo
cp /path/to/task.zip training-runs/demo/task.zip
uv run --python 3.12 --with-requirements requirements-ubuntu.txt \
  python ubuntu_training.py training-runs/demo/task.zip training-runs/demo/result.zip
```

另开一个终端启动监控：

```sh
uv run --python 3.12 --with-requirements requirements-ubuntu.txt python training_dashboard.py
```

打开 <http://127.0.0.1:8000/training>。页面显示训练损失、留出验证损失、梯度范数、学习率、轮次、耗时、预计剩余、峰值显存、日志和结果包下载。训练器会在结果 ZIP 旁写入 `progress.json` 和 `training.log`，页面每 2 秒刷新一次运行中的任务。

Ubuntu 是远端机器时，在 Mac 上建立 SSH 端口转发后打开同一个地址：

```sh
ssh -L 8000:127.0.0.1:8000 user@ubuntu
```

监控服务默认只监听 `127.0.0.1`；不建议直接暴露到局域网。

## 验证

```sh
uv run --python 3.12 --with-requirements requirements-ubuntu.txt python -m unittest -v
```

RTX 5060 8 GB 的跨机验收从短片段、rank 4、1 步开始。Mac 的 `test_training_hardware.py` 可用两台机器验证实际导出、训练、导入、试听、发布和聊天合成。

## 分支协作

`training_exchange.py` 和 `breeze_training.py` 是两边的共享代码。先在两个分支同步协议、摘要算法和 adapter 布局的对应变更，再跑跨机验收。平台专属代码在各自分支提交。
