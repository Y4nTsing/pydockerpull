import requests
import json
import os
import tarfile
import argparse
import urllib3
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

# 禁用 SSL 证书验证警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Token 交换流程 ──────────────────────────────────────────────

def get_bearer_token(harbor_host, project_name, image_name, portal="https",
                     verify_ssl=False):
    """
    Docker Registry V2 匿名 token 交换。
    公开项目的标准流程: 先发请求触发 401，从 Www-Authenticate 头
    解析 realm/service/scope，向 token 服务换取匿名 Bearer token。
    """
    url = f"{portal}://{harbor_host}/v2/{project_name}/{image_name}/manifests/latest"
    try:
        resp = requests.get(url, verify=verify_ssl, timeout=15)
    except requests.RequestException:
        return None

    if resp.status_code != 401:
        return None

    auth_header = resp.headers.get("www-authenticate", "")
    realm = re.search(r'realm="([^"]+)"', auth_header)
    if not realm:
        return None

    service = re.search(r'service="([^"]+)"', auth_header)
    scope = re.search(r'scope="([^"]+)"', auth_header)

    params = {}
    if service:
        params["service"] = service.group(1)
    if scope:
        params["scope"] = scope.group(1)

    try:
        token_resp = requests.get(realm.group(1), params=params,
                                  verify=verify_ssl, timeout=15)
    except requests.RequestException:
        return None

    if token_resp.status_code == 200:
        data = token_resp.json()
        return data.get("token") or data.get("access_token")
    return None


# ── 链接解析 ─────────────────────────────────────────────────────

def parse_docker_pull_link(pull_link):
    if "://" in pull_link:
        raise ValueError(
            "Docker pull link should not include protocol (e.g., http://).")

    parts = pull_link.split("/")
    if len(parts) < 3:
        raise ValueError(
            "Invalid Docker pull link format. "
            "Expected: <harbor-host>/<project>/<image>:<tag> "
            "or <harbor-host>/<project>/<image>/<name>@sha256:<digest>")

    harbor_host = parts[0]
    project_name = parts[1]
    image_and_ref = "/".join(parts[2:])

    if "@sha256:" in image_and_ref:
        image_name, image_ref = image_and_ref.split("@sha256:")
        image_ref = f"sha256:{image_ref}"
    elif ":" in image_and_ref:
        image_name, image_ref = image_and_ref.split(":")
    else:
        image_name = image_and_ref
        image_ref = "latest"

    return harbor_host, project_name, image_name, image_ref


# ── URL 构造 ─────────────────────────────────────────────────────

def get_manifest_url(harbor_host, project_name, image_name, image_ref,
                     portal="https"):
    return (f"{portal}://{harbor_host}/v2/"
            f"{project_name}/{image_name}/manifests/{image_ref}")


def get_blob_url(harbor_host, project_name, image_name, blob_digest,
                 portal="https"):
    return (f"{portal}://{harbor_host}/v2/"
            f"{project_name}/{image_name}/blobs/{blob_digest}")


# ── Manifest ─────────────────────────────────────────────────────

def get_manifest(harbor_host, project_name, image_name, image_ref,
                 auth=None, token=None, verify_ssl=False, hostname=None,
                 portal="https"):
    url = get_manifest_url(harbor_host, project_name, image_name,
                           image_ref, portal)
    req_headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
    if hostname:
        req_headers["Host"] = hostname
    if token:
        req_headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(url, headers=req_headers, auth=auth,
                        verify=verify_ssl, timeout=60)
    if resp.status_code != 200:
        raise Exception(
            f"Failed to fetch manifest: {resp.status_code} {resp.text[:500]}")
    return resp.json()


# ── Blob 下载 ────────────────────────────────────────────────────

def download_blob(harbor_host, project_name, image_name, blob_digest,
                  output_dir, auth=None, token=None, verify_ssl=False,
                  hostname=None, is_config=False, portal="https"):
    url = get_blob_url(harbor_host, project_name, image_name, blob_digest,
                       portal)
    req_headers = {}
    if hostname:
        req_headers["Host"] = hostname
    if token:
        req_headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(url, headers=req_headers, auth=auth, stream=True,
                        verify=verify_ssl, timeout=120)
    if resp.status_code != 200:
        raise Exception(
            f"Failed to download blob: {resp.status_code} {resp.text[:500]}")

    ext = ".json" if is_config else ".tar.gz"
    blob_path = os.path.join(output_dir, blob_digest.replace(":", "_") + ext)
    with open(blob_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return blob_path


# ── 鉴权决策 ─────────────────────────────────────────────────────

def resolve_auth(harbor_host, project_name, image_name, auth=None,
                 token=None, anonymous=False, portal="https",
                 verify_ssl=False):
    """
    确定最终使用的鉴权方式，返回 (effective_auth, effective_token)。
    优先级:
      1. 外部传入的 token
      2. 用户提供的 Basic Auth（失败则 fallback 匿名 token）
      3. 匿名 token（公开项目）
    """
    if token:
        print("Using provided Bearer token.")
        return None, token

    if auth and not anonymous:
        print(f"Trying Basic Auth (user={auth[0]})...")
        # 快速验证 Basic Auth 是否有效
        url = f"{portal}://{harbor_host}/v2/"
        req_headers = {}
        try:
            resp = requests.get(url, headers=req_headers, auth=auth,
                                verify=verify_ssl, timeout=15)
            if resp.status_code == 200:
                print("Basic Auth succeeded.")
                return auth, None
        except requests.RequestException:
            pass

        # Basic Auth 失败，尝试匿名 token
        print("Basic Auth rejected, falling back to anonymous token...")
        anon_token = get_bearer_token(harbor_host, project_name, image_name,
                                      portal=portal, verify_ssl=verify_ssl)
        if anon_token:
            print("Anonymous token obtained.")
            return None, anon_token
        raise Exception(
            "Authentication failed: Basic Auth rejected and no anonymous "
            "token available. This repository may require valid credentials.")

    # 匿名模式
    print("Fetching anonymous Bearer token...")
    anon_token = get_bearer_token(harbor_host, project_name, image_name,
                                  portal=portal, verify_ssl=verify_ssl)
    if anon_token:
        print("Anonymous token obtained.")
        return None, anon_token
    raise Exception(
        "No anonymous token available. The registry may require "
        "authentication. Try providing --username and --password.")


# ── 拉取镜像 ─────────────────────────────────────────────────────

def pull_image(harbor_host, project_name, image_name, image_ref,
               output_dir, auth=None, token=None, verify_ssl=False,
               hostname=None, portal="https", anonymous=False,
               workers=4):
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: 确定鉴权方式
    effective_auth, effective_token = resolve_auth(
        harbor_host, project_name, image_name, auth=auth, token=token,
        anonymous=anonymous, portal=portal, verify_ssl=verify_ssl)

    # Step 2: 获取 Manifest
    manifest = get_manifest(harbor_host, project_name, image_name,
                            image_ref, auth=effective_auth,
                            token=effective_token, verify_ssl=verify_ssl,
                            hostname=hostname, portal=portal)
    print("Manifest fetched successfully.")

    # Step 3+4: 并行下载 config + 全部 layer
    layers = manifest.get("layers", [])
    config_digest = manifest["config"]["digest"]

    # 收集待下载项（跳过已存在的文件）
    downloads = []
    ext = ".json"
    path = os.path.join(output_dir, config_digest.replace(":", "_") + ext)
    if not os.path.exists(path):
        downloads.append(("config", config_digest))

    for layer in layers:
        blob_digest = layer["digest"]
        ext = ".tar.gz"
        path = os.path.join(output_dir, blob_digest.replace(":", "_") + ext)
        if not os.path.exists(path):
            downloads.append(("layer", blob_digest))

    if downloads:
        n = len(downloads)
        print(f"Downloading {n} blob(s) with {workers} worker(s)...")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for dl_type, digest in downloads:
                is_config = (dl_type == "config")
                f = executor.submit(
                    download_blob,
                    harbor_host, project_name, image_name, digest,
                    output_dir,
                    auth=effective_auth, token=effective_token,
                    verify_ssl=verify_ssl, hostname=hostname,
                    is_config=is_config, portal=portal)
                futures[f] = digest

            for future in as_completed(futures):
                digest = futures[future]
                try:
                    future.result()
                except Exception as e:
                    raise Exception(
                        f"Failed to download blob {digest}: {e}") from e
    else:
        print("All blobs already downloaded (resuming).")

    # Step 5: 保存 Manifest
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f)
    print(f"Manifest saved to {manifest_path}")
    print("Image pulled successfully.")


# ── 打包 tar ─────────────────────────────────────────────────────

def create_image_tar(output_dir, tar_path, pull_link):
    manifest_path = os.path.join(output_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    if not isinstance(manifest, dict):
        raise ValueError("Manifest is not in expected format (dict).")
    if "config" not in manifest or "layers" not in manifest:
        raise ValueError("Manifest missing 'config' or 'layers'.")

    config_digest = manifest["config"]["digest"].replace(":", "_") + ".json"
    layer_files = [layer["digest"].replace(":", "_") + ".tar.gz"
                   for layer in manifest["layers"]]

    config_path = os.path.join(output_dir, config_digest)
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    for lf in layer_files:
        if not os.path.exists(os.path.join(output_dir, lf)):
            raise FileNotFoundError(f"Layer file not found: {lf}")

    if "@sha256:" in pull_link:
        repo, digest = pull_link.split("@sha256:")
        repo_tag = f"{repo}:sha256-{digest}"
    else:
        repo_tag = pull_link

    docker_manifest = [{
        "Config": config_digest,
        "RepoTags": [repo_tag],
        "Layers": layer_files,
    }]

    docker_manifest_path = os.path.join(output_dir, "manifest.json")
    with open(docker_manifest_path, "w") as f:
        json.dump(docker_manifest, f)

    with tarfile.open(tar_path, "w") as tar:
        tar.add(docker_manifest_path, arcname="manifest.json")
        tar.add(config_path, arcname=config_digest)
        for lf in layer_files:
            tar.add(os.path.join(output_dir, lf), arcname=lf)

    print(f"Image tar file created: {tar_path}")


# ── Layer 提取（无 Docker 直接导出文件系统）────────────────────────

def _apply_whiteout(target_dir, dirname, basename):
    if basename == '.wh..wh..opq':
        # Opaque whiteout: 清除该目录在下层中所有内容
        opaque_dir = os.path.join(target_dir, dirname)
        if os.path.isdir(opaque_dir):
            for entry in os.listdir(opaque_dir):
                entry_path = os.path.join(opaque_dir, entry)
                if os.path.isdir(entry_path) and not os.path.islink(entry_path):
                    shutil.rmtree(entry_path)
                else:
                    os.remove(entry_path)
        return

    # 常规 whiteout: .wh.<filename> 表示删除 <filename>
    if len(basename) <= 4 or not basename.startswith('.wh.'):
        return
    hidden_name = basename[4:]
    if not hidden_name:
        return
    hidden_path = os.path.join(target_dir, dirname, hidden_name)
    if os.path.lexists(hidden_path):
        if os.path.isdir(hidden_path) and not os.path.islink(hidden_path):
            shutil.rmtree(hidden_path)
        else:
            os.remove(hidden_path)


def extract_layer(tar_gz_path, target_dir):
    with tarfile.open(tar_gz_path, 'r:gz') as tar:
        for member in tar:
            dirname = os.path.dirname(member.name)
            basename = os.path.basename(member.name)

            if basename.startswith('.wh.'):
                _apply_whiteout(target_dir, dirname, basename)
                continue

            # 安全校验：防止路径穿越
            normalized = os.path.normpath(member.name)
            if os.path.isabs(normalized) or normalized.startswith('..'):
                print(f"  Skipping suspicious path in layer: {member.name}")
                continue

            tar.extract(member, target_dir, set_attrs=False)


def extract_layers_to_dir(output_dir, target_dir):
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    layers = manifest.get("layers", [])
    if not layers:
        print("No layers found in manifest.")
        return

    os.makedirs(target_dir, exist_ok=True)

    for i, layer in enumerate(layers, 1):
        blob_digest = layer["digest"].replace(":", "_") + ".tar.gz"
        blob_path = os.path.join(output_dir, blob_digest)

        if not os.path.exists(blob_path):
            print(f"Warning: layer {i}/{len(layers)} not found: {blob_path}")
            continue

        print(f"Extracting layer {i}/{len(layers)}: {blob_digest}")
        extract_layer(blob_path, target_dir)

    write_docker_history(output_dir, target_dir)
    print(f"Filesystem extracted to: {target_dir}")


def _format_size(bytes_val):
    if not bytes_val:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}" if unit != 'B' else f"{int(bytes_val)} B"
        bytes_val /= 1024
    return f"{bytes_val:.1f} PB"


def write_docker_history(output_dir, target_dir):
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    config_digest = manifest["config"]["digest"].replace(":", "_") + ".json"
    config_path = os.path.join(output_dir, config_digest)

    if not os.path.exists(config_path):
        print("Config file not found, skipping docker history.")
        return

    with open(config_path, "r") as f:
        config = json.load(f)

    history = config.get("history", [])
    layers = manifest.get("layers", [])

    # 非空 history 条目与 manifest layers 一一对应
    non_empty = [h for h in history if not h.get("empty_layer", False)]

    history_path = os.path.join(target_dir, "docker-history.txt")
    with open(history_path, "w") as f:
        f.write(f"{'CREATED':<30} {'CREATED BY':<55} {'SIZE':>12}\n")
        f.write("-" * 99 + "\n")

        layer_idx = 0
        for entry in history:
            created = entry.get("created", "").replace("T", " ").replace("Z", "")[:29]
            created_by = entry.get("created_by", "") or entry.get("comment", "")
            if len(created_by) > 55:
                created_by = created_by[:52] + "..."

            if entry.get("empty_layer", False):
                size = "0 B"
            else:
                if layer_idx < len(layers):
                    size = _format_size(layers[layer_idx].get("size", 0))
                    layer_idx += 1
                else:
                    size = "0 B"

            f.write(f"{created:<30} {created_by:<55} {size:>12}\n")

    print(f"Docker history written to: {history_path}")


# ── 主入口 ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pull a Docker image from Harbor (public or private) "
                    "and save as tar.")
    parser.add_argument("pull_link", type=str,
                        help="Docker pull link, e.g. "
                             "registry.example.com/project/image:tag "
                             "or .../image@sha256:digest")
    parser.add_argument("--username", type=str, default=None,
                        help="Harbor username (for private repos)")
    parser.add_argument("--password", type=str, default=None,
                        help="Harbor password (for private repos)")
    parser.add_argument("--token", type=str, default=None,
                        help="Pre-obtained Bearer token")
    parser.add_argument("--anonymous", action="store_true",
                        help="Force anonymous access (ignore --username/--password)")
    parser.add_argument("--output-dir", type=str, default="output_image",
                        help="Temp dir for downloaded files (deleted after tar)")
    parser.add_argument("--extract-to", type=str, default=None,
                        help="Extract merged filesystem directly to this dir "
                             "(no Docker needed)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel download threads (default: 4)")
    parser.add_argument("--verify-ssl", action="store_true",
                        help="Verify SSL certificate (default: False)")
    parser.add_argument("--hostname", type=str,
                        help="Custom Host header for requests")
    parser.add_argument("--portal", type=str, default="https",
                        choices=["http", "https"],
                        help="Protocol: http or https (default: https)")
    args = parser.parse_args()

    # ── 解析链接 ──
    try:
        harbor_host, project_name, image_name, image_ref = \
            parse_docker_pull_link(args.pull_link)
        print(f"Parsed: Host={harbor_host}, Project={project_name}, "
              f"Image={image_name}, Ref={image_ref}")
    except ValueError as e:
        print(f"Error: {e}")
        return

    # ── 鉴权准备 ──
    auth = None
    if args.anonymous:
        print("Forcing anonymous access.")
    elif args.username and args.password:
        auth = (args.username, args.password)
    elif args.username or args.password:
        print("Warning: both --username and --password required for "
              "Basic Auth. Falling back to anonymous.")
    else:
        print("No credentials provided, using anonymous access.")

    verify_ssl = args.verify_ssl
    hostname = args.hostname
    portal = args.portal

    # ── 拉取 ──
    try:
        pull_image(harbor_host, project_name, image_name, image_ref,
                   args.output_dir, auth=auth, token=args.token,
                   verify_ssl=verify_ssl, hostname=hostname,
                   portal=portal, anonymous=args.anonymous,
                   workers=args.workers)
    except Exception as e:
        print(f"Failed to pull image: {e}")
        return

    # ── 提取文件系统（无 Docker）──
    if args.extract_to:
        try:
            extract_layers_to_dir(args.output_dir, args.extract_to)
        except Exception as e:
            print(f"Failed to extract layers: {e}")
            return

    # ── 打包 ──
    tar_path = f"{image_name.replace('/', '_')}_{image_ref.replace(':', '_')}.tar"
    try:
        create_image_tar(args.output_dir, tar_path, args.pull_link)
    except Exception as e:
        print(f"Failed to create tar: {e}")
        return

    # ── 清理 ──
    try:
        shutil.rmtree(args.output_dir)
        print(f"Temp dir '{args.output_dir}' deleted.")
    except Exception as e:
        print(f"Warning: could not delete temp dir: {e}")


if __name__ == "__main__":
    main()
