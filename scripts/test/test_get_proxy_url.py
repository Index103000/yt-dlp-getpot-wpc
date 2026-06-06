#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import requests


# PROXY_API_URL = "http://43.132.108.19:10089/api/get_sdk_host_list"
PROXY_API_URL = "http://10.0.224.68:10089/api/get_sdk_host_list"

# hk / xg
REGION = "xmpdk"


def main():
    payload = {
        "region": REGION,
    }

    print(f"[request] POST {PROXY_API_URL}")
    print(f"[request] payload={payload}")

    try:
        resp = requests.post(
            PROXY_API_URL,
            data=payload,
            timeout=10,
        )

        print(f"[response] status_code={resp.status_code}")

        resp.raise_for_status()

        result = resp.json()

        print(
            "[response] body=\n"
            + json.dumps(
                result,
                ensure_ascii=False,
                indent=4,
            )
        )

        if result.get("code") != 200:
            print(f"[error] 接口返回失败: {result}")
            return

        data = result.get("data") or {}

        proxy_template_url = data.get("curl_str")
        ep_url = data.get("ep_url")

        print()
        print("========== RESULT ==========")
        print(f"proxy_template_url = {proxy_template_url}")
        print(f"ep_url             = {ep_url}")

    except Exception as e:
        print(f"[error] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()