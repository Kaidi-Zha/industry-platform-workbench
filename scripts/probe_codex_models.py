"""Print the live model/list catalog from the signed-in local Codex CLI."""

import argparse
import json
import logging
import subprocess

from industplatform import codex_local


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="", help="本机 Codex CLI 的绝对路径，默认自动检测")
    parser.add_argument("--client-name", default=codex_local.CLIENT_INFO["name"],
                        help="用于比对 model/list 的 clientInfo.name")
    parser.add_argument("--include-hidden", action="store_true", help="诊断时一并读取隐藏模型")
    parser.add_argument("--summary", action="store_true", help="每个模型仅打印 model、displayName 和 isDefault")
    parser.add_argument("--trace-rpc", action="store_true", help="将每页原始 model/list RPC 响应写到 stderr")
    args = parser.parse_args()
    if args.trace_rpc:
        logging.basicConfig(format="%(message)s")
        logging.getLogger(codex_local.__name__).setLevel(logging.DEBUG)
    try:
        install = codex_local.find_binary(args.codex)
        if install is None:
            parser.exit(1, codex_local.status_message("cli_missing") + "\n")
        with codex_local.account_session(install.path, client_name=args.client_name) as session:
            account = codex_local.read_account(session)
            if account["status"] != "ready_subscription":
                raise codex_local.LocalCodexError(codex_local.status_message(account["status"]))
            models = codex_local.model_catalog_rows(session, include_hidden=args.include_hidden)
    except codex_local.LocalCodexError as exc:
        parser.exit(1, str(exc) + "\n")
    except (OSError, subprocess.SubprocessError):
        parser.exit(1, "无法启动本机 Codex App Server。\n")
    if args.summary:
        models = [{key: row.get(key) for key in ("model", "displayName", "isDefault")} for row in models]
    print(json.dumps({"binary_path": install.path, "version": install.version, "catalog_source": codex_local.CATALOG_SOURCE,
                      "client_name": args.client_name, "include_hidden": args.include_hidden,
                      "data": models, "nextCursor": None}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
