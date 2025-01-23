import requests
import json
import os
import tarfile
import argparse
import urllib3

# 禁用 SSL 证书验证警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 解析 Docker Pull 链接
def parse_docker_pull_link(pull_link):
    """
    解析 Docker Pull 链接，提取 Harbor 地址、项目名称、镜像名称和标签或摘要。
    示例链接：
    - harbor.example.com/myproject/nginx:latest
    - harbor.example.com/myproject/random-image/random-name@sha256:randomdigest
    """
    if "://" in pull_link:
        raise ValueError("Docker pull link should not include protocol (e.g., http://).")

    parts = pull_link.split("/")
    if len(parts) < 3:
        raise ValueError("Invalid Docker pull link format. Expected: <harbor-host>/<project>/<image>:<tag> or <harbor-host>/<project>/<image>/<image-name>@sha256:<digest>")

    harbor_host = parts[0]
    project_name = parts[1]
    image_and_ref = "/".join(parts[2:])  # 处理多层镜像名称

    # 判断是标签还是摘要
    if "@sha256:" in image_and_ref:
        image_name, image_ref = image_and_ref.split("@sha256:")
        image_ref = f"sha256:{image_ref}"  # 确保摘要格式正确
    elif ":" in image_and_ref:
        image_name, image_ref = image_and_ref.split(":")
    else:
        image_name = image_and_ref
        image_ref = "latest"  # 默认标签

    return harbor_host, project_name, image_name, image_ref

# 获取 Manifest 的 URL
def get_manifest_url(harbor_host, project_name, image_name, image_ref):
    return f"https://{harbor_host}/v2/{project_name}/{image_name}/manifests/{image_ref}"

# 获取 Blob（层）的 URL
def get_blob_url(harbor_host, project_name, image_name, blob_digest):
    return f"https://{harbor_host}/v2/{project_name}/{image_name}/blobs/{blob_digest}"

# 获取镜像的 Manifest
def get_manifest(harbor_host, project_name, image_name, image_ref, auth=None, verify_ssl=False, hostname=None):
    url = get_manifest_url(harbor_host, project_name, image_name, image_ref)
    headers = {
        "Accept": "application/vnd.docker.distribution.manifest.v2+json"
    }
    if hostname:
        headers["Host"] = hostname
    response = requests.get(url, headers=headers, auth=auth, verify=verify_ssl)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch manifest: {response.status_code} {response.text}")
    return response.json()

# 下载 Blob（层或 Config 文件）
def download_blob(harbor_host, project_name, image_name, blob_digest, output_dir, auth=None, verify_ssl=False, hostname=None, is_config=False):
    url = get_blob_url(harbor_host, project_name, image_name, blob_digest)
    headers = {}
    if hostname:
        headers["Host"] = hostname
    response = requests.get(url, headers=headers, auth=auth, stream=True, verify=verify_ssl)
    if response.status_code != 200:
        raise Exception(f"Failed to download blob: {response.status_code} {response.text}")

    # 保存 Blob 到文件
    if is_config:
        # Config 文件保存为 .json
        blob_path = os.path.join(output_dir, blob_digest.replace(":", "_") + ".json")
    else:
        # 层文件保存为 .tar.gz
        blob_path = os.path.join(output_dir, blob_digest.replace(":", "_") + ".tar.gz")
    
    with open(blob_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return blob_path

# 拉取镜像并保存为 tar 文件
def pull_image(harbor_host, project_name, image_name, image_ref, output_dir, auth=None, verify_ssl=False, hostname=None):
    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)

    # 获取 Manifest
    try:
        manifest = get_manifest(harbor_host, project_name, image_name, image_ref, auth, verify_ssl, hostname)
        print("Manifest fetched successfully.")
    except Exception as e:
        print(f"Failed to fetch manifest: {e}")
        return

    # 下载 Config 文件
    config_digest = manifest["config"]["digest"]
    config_file = config_digest.replace(":", "_") + ".json"
    config_path = os.path.join(output_dir, config_file)
    if not os.path.exists(config_path):
        print(f"Downloading config file: {config_digest}")
        try:
            download_blob(harbor_host, project_name, image_name, config_digest, output_dir, auth, verify_ssl, hostname, is_config=True)
        except Exception as e:
            print(f"Failed to download config file: {e}")
            return

    # 下载所有层文件
    layers = manifest.get("layers", [])
    for layer in layers:
        blob_digest = layer["digest"]
        print(f"Downloading layer: {blob_digest}")
        try:
            download_blob(harbor_host, project_name, image_name, blob_digest, output_dir, auth, verify_ssl, hostname)
        except Exception as e:
            print(f"Failed to download layer {blob_digest}: {e}")
            return

    # 保存 Manifest
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f)
    print(f"Manifest saved to {manifest_path}")

    print("Image pulled successfully.")

# 将下载的镜像文件打包为 tar 文件
def create_image_tar(output_dir, tar_path, pull_link):
    """
    将下载的镜像文件打包为 Docker 镜像的 tar 文件。
    :param output_dir: 下载的镜像文件目录
    :param tar_path: 输出的 tar 文件路径
    :param pull_link: Docker pull 链接，用于生成 RepoTags
    """
    # 读取 Manifest 文件
    manifest_path = os.path.join(output_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    with open(manifest_path, "r") as f:
        try:
            manifest = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Manifest file is not valid JSON: {e}")

    if not manifest:
        raise ValueError("Manifest file is empty or invalid.")

    # 检查 Manifest 格式
    if not isinstance(manifest, dict):
        raise ValueError("Manifest file is not in the expected format (expected a dictionary).")

    if "config" not in manifest or "layers" not in manifest:
        raise ValueError("Manifest file is not in the expected format (missing 'config' or 'layers').")

    # 获取 Config 文件和层文件
    config_digest = manifest["config"]["digest"].replace(":", "_") + ".json"
    layer_files = [layer["digest"].replace(":", "_") + ".tar.gz" for layer in manifest["layers"]]

    # 检查 Config 文件是否存在
    config_path = os.path.join(output_dir, config_digest)
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # 检查层文件是否存在
    for layer_file in layer_files:
        layer_path = os.path.join(output_dir, layer_file)
        if not os.path.exists(layer_path):
            raise FileNotFoundError(f"Layer file not found: {layer_path}")

    # 处理 pull_link，生成合法的 RepoTags
    if "@sha256:" in pull_link:
        # 如果 pull_link 包含 @sha256:，将其转换为合法的标签格式
        repo, digest = pull_link.split("@sha256:")
        repo_tag = f"{repo}:sha256-{digest}"  # 将 @sha256: 替换为 :sha256-
    else:
        # 如果 pull_link 是标准的 <repository>:<tag> 格式，直接使用
        repo_tag = pull_link

    # 生成符合 Docker 镜像 tar 文件格式的 manifest.json
    docker_manifest = [
        {
            "Config": config_digest,
            "RepoTags": [repo_tag],  # 使用处理后的 repo_tag
            "Layers": layer_files
        }
    ]

    # 保存新的 manifest.json
    docker_manifest_path = os.path.join(output_dir, "manifest.json")
    with open(docker_manifest_path, "w") as f:
        json.dump(docker_manifest, f)

    # 创建 tar 文件
    with tarfile.open(tar_path, "w") as tar:
        # 添加新的 manifest.json
        tar.add(docker_manifest_path, arcname="manifest.json")

        # 添加 Config 文件
        tar.add(config_path, arcname=config_digest)

        # 添加层文件
        for layer_file in layer_files:
            layer_path = os.path.join(output_dir, layer_file)
            tar.add(layer_path, arcname=layer_file)

    print(f"Image tar file created successfully: {tar_path}")

# 主函数
def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="Pull a Docker image from Harbor and save it as a tar file.")
    parser.add_argument("pull_link", type=str, help="Docker pull link (e.g., harbor.example.com/myproject/nginx:latest or harbor.example.com/myproject/random-image/random-name@sha256:randomdigest)")
    parser.add_argument("--username", type=str, default="admin", help="Harbor username (default: admin)")
    parser.add_argument("--password", type=str, default="Harbor12345", help="Harbor password (default: Harbor12345)")
    parser.add_argument("--output-dir", type=str, default="output_image", help="Output directory for the pulled image")
    parser.add_argument("--verify-ssl", action="store_true", help="Verify SSL certificate (default: False)")
    parser.add_argument("--hostname", type=str, help="Custom Host header for requests (e.g., harbor.example.com)")
    args = parser.parse_args()

    # 解析 Docker Pull 链接
    try:
        harbor_host, project_name, image_name, image_ref = parse_docker_pull_link(args.pull_link)
        print(f"Parsed Docker pull link: Host={harbor_host}, Project={project_name}, Image={image_name}, Ref={image_ref}")
    except ValueError as e:
        print(f"Error parsing Docker pull link: {e}")
        return

    # 认证信息（默认使用 admin/Harbor12345）
    auth = (args.username, args.password)
    print(f"Using authentication: username={args.username}, password=******")

    # SSL 证书验证
    verify_ssl = args.verify_ssl
    print(f"SSL certificate verification: {verify_ssl}")

    # 自定义 Host 头
    hostname = args.hostname
    if hostname:
        print(f"Using custom Host header: {hostname}")

    # 拉取镜像
    try:
        pull_image(harbor_host, project_name, image_name, image_ref, args.output_dir, auth, verify_ssl, hostname)
    except Exception as e:
        print(f"Failed to pull image: {e}")
        return

    # 打包镜像为 tar 文件
    tar_path = f"{image_name.replace('/', '_')}_{image_ref.replace(':', '_')}.tar"
    try:
        create_image_tar(args.output_dir, tar_path, args.pull_link)
    except Exception as e:
        print(f"Failed to create image tar file: {e}")

if __name__ == "__main__":
    main()