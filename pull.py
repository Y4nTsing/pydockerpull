import requests
import json
import os
import tarfile
import argparse
import urllib3
import re
import shutil

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
               hostname=None, portal="https", anonymous=False):
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

    # Step 3: 下载 Config
    config_digest = manifest["config"]["digest"]
    config_file = config_digest.replace(":", "_") + ".json"
    config_path = os.path.join(output_dir, config_file)
    if not os.path.exists(config_path):
        print(f"Downloading config file: {config_digest}")
        download_blob(harbor_host, project_name, image_name, config_digest,
                      output_dir, auth=effective_auth, token=effective_token,
                      verify_ssl=verify_ssl, hostname=hostname, is_config=True,
                      portal=portal)

    # Step 4: 下载所有层
    for layer in manifest.get("layers", []):
        blob_digest = layer["digest"]
        print(f"Downloading layer: {blob_digest}  "
              f"({layer.get('size', '?')} bytes)")
        download_blob(harbor_host, project_name, image_name, blob_digest,
                      output_dir, auth=effective_auth, token=effective_token,
                      verify_ssl=verify_ssl, hostname=hostname, portal=portal)

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
                   portal=portal, anonymous=args.anonymous)
    except Exception as e:
        print(f"Failed to pull image: {e}")
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
