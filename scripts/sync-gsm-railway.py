#!/usr/bin/env python3
"""
Sync secrets from Google Secret Manager (GSM) to Railway Shared Variables.
Requires:
  - gcloud CLI authenticated (via google-github-actions/auth or local gcloud auth)
  - Environment variables:
      GCP_PROJECT_ID
      RAILWAY_TOKEN
      RAILWAY_PROJECT_ID
      RAILWAY_ENVIRONMENT_ID (required; can be name like 'production' or UUID)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

RAILWAY_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"


def run_graphql(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    
    header_candidates = [
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "vox-gsm-railway-sync/1.0",
        },
        {
            "Project-Access-Token": token,
            "Content-Type": "application/json",
            "User-Agent": "vox-gsm-railway-sync/1.0",
        },
    ]

    last_error = None
    for headers in header_candidates:
        req = urllib.request.Request(RAILWAY_GRAPHQL_URL, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "errors" in data:
                    err_msg = json.dumps(data["errors"])
                    if "Not Authorized" in err_msg or "unauthorized" in err_msg.lower():
                        last_error = RuntimeError(
                            f"Railway API returned Not Authorized.\n"
                            "Verify token scope and target identifiers."
                        )
                        continue
                    raise RuntimeError("Railway GraphQL request failed; inspect target state before retrying.")
                return data.get("data", {})
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                last_error = e
                continue
            raise RuntimeError(f"Railway HTTP {e.code}; inspect target state before retrying.") from None

    if last_error:
        raise last_error
    return {}


def resolve_railway_environment(token: str, project_id: str, requested_env: str | None) -> tuple[str, str]:
    query = """
    query GetProject($id: String!) {
      project(id: $id) {
        id
        name
        environments {
          edges {
            node {
              id
              name
            }
          }
        }
      }
    }
    """
    data = run_graphql(token, query, {"id": project_id})
    proj = data.get("project")
    if not proj:
        raise RuntimeError(
            f"Project with ID '{project_id}' was not found or the provided token does not have access to it.\n"
            f"Please verify RAILWAY_PROJECT_ID (copy Project ID from Railway project settings) "
            f"and ensure your RAILWAY_TOKEN workspace scope includes this project."
        )

    proj_name = proj.get("name", "Unknown")
    print(f"✓ Connected to Railway project: '{proj_name}' (ID: {proj.get('id')})")

    environments = [
        edge["node"] for edge in proj.get("environments", {}).get("edges", [])
    ]
    if not environments:
        raise RuntimeError(f"No environments found in Railway project '{proj_name}'.")

    matches = [env for env in environments
               if env["id"] == requested_env or env["name"] == requested_env]
    if not requested_env or len(matches) != 1:
        raise RuntimeError("RAILWAY_ENVIRONMENT_ID must identify exactly one environment; no fallback is allowed.")
    return matches[0]["id"], matches[0]["name"]


def selected_keys(value: str | None) -> list[str]:
    keys = [key.strip() for key in (value or "").split(",")]
    if not keys or any(not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("SECRET_KEYS must contain an explicit nonempty unique allowlist.")
    import re
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in keys):
        raise ValueError("SECRET_KEYS entries must be exact environment-variable names.")
    return keys


def fetch_gsm_secret(gcp_project: str, secret_name: str) -> str | None:
    try:
        cmd = [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={secret_name}",
            f"--project={gcp_project}",
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError:
        return None


def upsert_railway_shared_variables(token: str, project_id: str, environment_id: str, variables: dict):
    # Try batch upsert first
    batch_mutation = """
    mutation UpsertSharedVariables($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }
    """
    payload = {
        "input": {
            "projectId": project_id,
            "environmentId": environment_id,
            "variables": variables,
            "replace": False,
        }
    }
    try:
        run_graphql(token, batch_mutation, payload)
        print("✓ Successfully upserted all shared variables via batch API!")
        return
    except Exception as e:
        print("Batch upsert failed; attempting explicitly selected variables individually.")

    # Fallback: upsert one by one
    single_mutation = """
    mutation UpsertSingleVariable($input: VariableUpsertInput!) {
      variableUpsert(input: $input)
    }
    """
    success_count = 0
    for name, value in variables.items():
        single_payload = {
            "input": {
                "projectId": project_id,
                "environmentId": environment_id,
                "name": name,
                "value": value,
            }
        }
        try:
            run_graphql(token, single_mutation, single_payload)
            print(f"  ✓ Upserted {name}")
            success_count += 1
        except Exception as err:
            print(f"  ✗ Failed to upsert {name}")

    if success_count != len(variables):
        raise RuntimeError(f"Incomplete sync: {success_count}/{len(variables)} variables written; inspect target state before retrying.")
    print(f"✓ Completed: upserted {success_count}/{len(variables)} variables.")


def main():
    gcp_project = os.environ.get("GCP_PROJECT_ID")
    railway_token = os.environ.get("RAILWAY_TOKEN")
    railway_project_id = os.environ.get("RAILWAY_PROJECT_ID")
    railway_env_input = os.environ.get("RAILWAY_ENVIRONMENT_ID")

    if not gcp_project:
        sys.exit("Error: GCP_PROJECT_ID environment variable is missing.")
    if not railway_token:
        sys.exit("Error: RAILWAY_TOKEN environment variable is missing.")
    if not railway_project_id:
        sys.exit("Error: RAILWAY_PROJECT_ID environment variable is missing.")

    keys_to_fetch = selected_keys(os.environ.get("SECRET_KEYS"))
    if not railway_env_input:
        sys.exit("Error: RAILWAY_ENVIRONMENT_ID must be explicit.")

    print("Verifying Railway project and resolving environment...")
    env_id, env_name = resolve_railway_environment(railway_token, railway_project_id, railway_env_input)
    print(f"Target Environment: '{env_name}' (ID: {env_id})")

    print(f"Using explicitly selected keys ({len(keys_to_fetch)} keys).")

    collected_secrets = {}
    print(f"\nFetching secrets from Google Secret Manager (Project: {gcp_project})...")
    for key in keys_to_fetch:
        val = fetch_gsm_secret(gcp_project, key)
        if val is not None:
            collected_secrets[key] = val
            print(f"  ✓ Fetched {key}")
        else:
            raise RuntimeError(f"Cannot read selected secret {key}; no variables have been written.")

    if not collected_secrets:
        print("No secrets found to sync.")
        return

    print(f"\nPushing {len(collected_secrets)} shared variable(s) to Railway environment '{env_name}'...")
    upsert_railway_shared_variables(railway_token, railway_project_id, env_id, collected_secrets)


if __name__ == "__main__":
    main()
