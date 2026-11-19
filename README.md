# Breeze-TTS-2 · Ubuntu CUDA 训练

`ubuntu` 分支运行 CUDA LoRA 训练。Mac 工作台、素材审核、试听、发布和聊天在 [`main` 分支](https://github.com/zchengyu854/Sound/tree/main)。

## 运行

在 Ubuntu 安装支持 CUDA 13 的 NVIDIA 驱动和 `uv`。从 Mac 的 `/training/` 下载冻结任务 ZIP，把它传到 Ubuntu 后执行：

```sh
uv run --python 3.12 --with-requirements requirements-ubuntu.txt \
  python ubuntu_training.py /path/to/task.zip /path/to/result.zip
```

使用已缓存的同一基座时可追加 `--base-dir /path/to/base`。终端显示步数、损失和 MLX 峰值显存。用同一个任务 ZIP 和输出目录再次运行会恢复最近一次完整检查点。不同任务使用不同的结果文件名。

训练结束后，把结果 ZIP 传回 Mac，在原任务中选择「导入 Ubuntu 结果包」。Mac 校验快照、配置和基座权重，再生成 A/B 试听。取消远端训练使用 Ubuntu 终端的 Ctrl+C。

## 验证

```sh
uv run --python 3.12 --with-requirements requirements-ubuntu.txt python -m unittest -v
```

RTX 5060 8 GB 的跨机验收从短片段、rank 4、1 步开始。Mac 的 `test_training_hardware.py` 可用两台机器验证实际导出、训练、导入、试听、发布和聊天合成。

## 分支协作

`training_exchange.py` 和 `breeze_training.py` 是两边的共享代码。先在两个分支同步协议、摘要算法和 adapter 布局的对应变更，再跑跨机验收。平台专属代码在各自分支提交。
