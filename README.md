# pydockerpull

纯 Python 脚本，无需 Docker 即可从 Harbor 仓库拉取镜像并导出文件系统。

## 快速开始

```bash
# 拉取并生成 docker load 用的 tar
python3 pull.py registry.example.com/project/image:latest

# 一步到位：直接导出完整文件系统（无需 Docker）
python3 pull.py registry.example.com/project/image:latest --extract-to ./rootfs
```

## 支持的链接格式

```
<harbor-host>/<project>/<image>:<tag>
<harbor-host>/<project>/<image>/<image-name>@sha256:<digest>
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `pull_link` | Docker pull 链接 | 必填 |
| `--username` | Harbor 用户名（私有仓库） | — |
| `--password` | Harbor 密码（私有仓库） | — |
| `--token` | 预获取的 Bearer token | — |
| `--anonymous` | 强制匿名访问（忽略用户名密码） | — |
| `--output-dir` | 下载临时目录 | `output_image` |
| `--extract-to` | 直接导出文件系统到此目录（无需 Docker） | — |
| `--workers` | 并行下载线程数 | `4` |
| `--verify-ssl` | 验证 SSL 证书 | `False` |
| `--hostname` | 自定义 Host 请求头 | — |
| `--portal` | 协议 http / https | `https` |

## 使用示例

### 公开仓库
```bash
python3 pull.py 10.0.0.1/library/nginx:alpine --extract-to ./nginx-rootfs
```

### 私有仓库（Basic Auth）
```bash
python3 pull.py harbor.example.com/private/app:v1.0 --username admin --password xxxx
```

### 指定 Host 头（解决 Harbor 绑定 0.0.0.0 问题）
```bash
python3 pull.py 10.0.0.1/project/image:tag --hostname harbor.example.com
```

### 高性能拉取（8 线程并行 + 直接导出）
```bash
python3 pull.py registry.example.com/proj/large-image:latest --workers 8 --extract-to ./fs
```

## 两种输出方式

### 方式一：docker load 兼容 tar
```bash
python3 pull.py x.x.x.x/aaa/bbb:latest
docker load -i bbb_latest.tar
# 然后 docker create / docker export 提取文件
```

### 方式二：直接导出文件系统
```bash
python3 pull.py x.x.x.x/aaa/bbb:latest --extract-to ./rootfs
# ./rootfs 即为完整容器文件系统，无需 Docker
```

方式二通过合并所有 layer（自动处理 whiteout 删除标记）直接重建文件系统，省去 `docker load → docker create → docker export → tar -xf` 的繁琐流程。

导出目录中会自动生成 `docker-history.txt`，记录每一层的创建时间和构建指令（等效 `docker history`）。

## 断点续传

下载中断后重跑同一命令，已下载的 blob 会被跳过，自动续传。
