"""
Docker 沙箱配置说明。

smolagents 内置了 executor_type="docker"，但默认镜像可能没装你需要的依赖。
生产环境建议自己构建镜像并把 skills 目录挂载进去。

## 自定义镜像

写一个 Dockerfile:

```dockerfile
FROM python:3.11-slim

RUN pip install --no-cache-dir \
    smolagents \
    pandas numpy \
    # 你的其他依赖

# 把 skills 目录拷贝进镜像，或者运行时挂载
WORKDIR /app
ENV PYTHONPATH=/app

CMD ["python", "-c", "import smolagents"]
```

构建：
```bash
docker build -t agent-sandbox:latest .
```

## 安全约束

smolagents 默认 Docker executor 会配置：
- mem_limit=512m
- cpu_quota=50000  (50% CPU)
- pids_limit=100
- security_opt=["no-new-privileges"]
- cap_drop=["ALL"]

如果需要进一步定制（比如挂载 skills 目录、调整资源限额），可以继承
smolagents.local_python_executor.DockerExecutor 自己写一个。

## 切换方式

main.py 里 `use_docker=True` 即可。注意：
- skill 内的 scripts 需要能在容器里 import，建议把 skills 目录通过
  volume 挂载到容器的 PYTHONPATH 下
- 内网推理服务的地址在容器内要可达（network 配置）
"""
