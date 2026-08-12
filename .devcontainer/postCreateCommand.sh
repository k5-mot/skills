#!/usr/bin/env bash

export TIMESTAMP
export LOG_DIR

TIMESTAMP=$(date +%s)
LOG_DIR="${HOME}/.devcontainer/logs/${TIMESTAMP}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(pwd)}"

# セットアップ対象外の重い生成物や依存ディレクトリをまとめて扱う。
readonly FIND_PRUNE_EXPR=(
    -path "${WORKSPACE_ROOT}/.git"
    -o -path "${WORKSPACE_ROOT}/.agents"
    -o -path "${WORKSPACE_ROOT}/.cache"
    -o -path "${WORKSPACE_ROOT}/.devcontainer"
    -o -path "${WORKSPACE_ROOT}/.pnpm-store"
    -o -path "${WORKSPACE_ROOT}/.serena"
    -o -path "${WORKSPACE_ROOT}/.venv"
    -o -path "${WORKSPACE_ROOT}/node_modules"
    -o -path "${WORKSPACE_ROOT}/*/.git"
    -o -path "${WORKSPACE_ROOT}/*/.cache"
    -o -path "${WORKSPACE_ROOT}/*/.pnpm-store"
    -o -path "${WORKSPACE_ROOT}/*/.serena"
    -o -path "${WORKSPACE_ROOT}/*/.venv"
    -o -path "${WORKSPACE_ROOT}/*/node_modules"
    -o -path "${WORKSPACE_ROOT}/*/dist"
    -o -path "${WORKSPACE_ROOT}/*/build"
    -o -path "${WORKSPACE_ROOT}/*/.next"
    -o -path "${WORKSPACE_ROOT}/*/.turbo"
)

# ディレクトリの所有権を現在のユーザーへ寄せる。
# Args:
#   $1: 所有者を変更するディレクトリ。
# Returns:
#   なし。
# Side Effects:
#   対象ディレクトリが存在する場合、sudo chown を実行する。
function chown_dir() {
    local dir=$1

    if [ -d "$dir" ]; then
        sudo chown -R "$(whoami):$(whoami)" "$dir"
    fi
}

# ログファイル名に使える安全な相対パス表現を返す。
# Args:
#   $1: プロジェクトディレクトリ。
# Returns:
#   ログファイル名に利用できる文字列。
function log_name_for_dir() {
    local project_dir=$1
    local relative_dir

    relative_dir=$(realpath --relative-to="${WORKSPACE_ROOT}" "$project_dir" 2>/dev/null || printf "%s" "$project_dir")
    if [ "$relative_dir" = "." ]; then
        relative_dir="root"
    fi

    printf "%s" "$relative_dir" | tr '/ ' '__'
}

# 指定ディレクトリの祖先に対象ファイルがあるか調べる。
# Args:
#   $1: 探索を開始するディレクトリ。
#   $2: 探すファイル名。
# Returns:
#   見つかった場合は 0、見つからない場合は 1。
function has_ancestor_file() {
    local dir
    local marker=$2

    dir=$(realpath "$1")
    while [ "$dir" != "${WORKSPACE_ROOT}" ] && [ "$dir" != "/" ]; do
        dir=$(dirname "$dir")
        if [ -f "${dir}/${marker}" ]; then
            return 0
        fi
    done

    return 1
}

# 複数markerから重複のないプロジェクトディレクトリ一覧を返す。
# Args:
#   $@: marker file names.
# Returns:
#   1行1ディレクトリの絶対パス一覧。
function discover_project_dirs() {
    local marker
    local file_path
    local project_dir
    declare -A seen=()

    for marker in "$@"; do
        while IFS= read -r -d '' file_path; do
            project_dir=$(dirname "$file_path")
            project_dir=$(realpath "$project_dir")
            if [ -z "${seen[$project_dir]+x}" ]; then
                seen[$project_dir]=1
                printf "%s\n" "$project_dir"
            fi
        done < <(find "${WORKSPACE_ROOT}" \( "${FIND_PRUNE_EXPR[@]}" \) -prune -o -name "$marker" -type f -print0)
    done
}

# Python プロジェクトを依存ファイルに応じてセットアップする。
# Args:
#   $1: プロジェクトディレクトリ。
# Returns:
#   セットアップ成功時は 0、失敗時は非0。
# Side Effects:
#   Python依存関係をインストールし、ログファイルを作成する。
function setup_python_project() {
    local project_dir=$1
    local log_file
    local python_version

    log_file="${LOG_DIR}/$(log_name_for_dir "$project_dir").python.log"
    mkdir -p "${LOG_DIR}"
    chown_dir "${project_dir}/.venv"

    pushd "$project_dir" >> "${log_file}" 2>&1 || return 1
    if [ -f "poetry.lock" ]; then
        poetry install >> "${log_file}" 2>&1
        printf "\e[36m- Completed to setup the poetry project.: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    elif [ -f "pyproject.toml" ] || [ -f "uv.lock" ]; then
        if [ -f ".python-version" ]; then
            python_version=$(tr -d '[:space:]' < .python-version)
            uv python install "$python_version" >> "${log_file}" 2>&1
        fi
        uv sync --dev >> "${log_file}" 2>&1
        printf "\e[36m- Completed to setup the uv(Python) project.: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    elif [ -f "requirements.txt" ]; then
        pip install -r requirements.txt >> "${log_file}" 2>&1
        printf "\e[36m- Completed to setup the pip project.: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    else
        printf "\e[33m- Skipped to setup Python project...: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    fi
    popd >> "${log_file}" 2>&1 || return 1
}

# Node.js プロジェクトを利用中のパッケージマネージャーに合わせてセットアップする。
# Args:
#   $1: プロジェクトディレクトリ。
# Returns:
#   セットアップ成功時は 0、失敗時は非0。
# Side Effects:
#   Node.js依存関係をインストールし、ログファイルを作成する。
function setup_nodejs_project() {
    local project_dir=$1
    local log_file

    log_file="${LOG_DIR}/$(log_name_for_dir "$project_dir").node.log"
    mkdir -p "${LOG_DIR}"
    chown_dir "${project_dir}/node_modules"

    pushd "$project_dir" >> "${log_file}" 2>&1 || return 1
    if [ -f "pnpm-lock.yaml" ] || [ -f "pnpm-workspace.yaml" ]; then
        pnpm install >> "${log_file}" 2>&1
        printf "\e[36m- Completed to setup the pnpm project.: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    elif [ -f "yarn.lock" ]; then
        yarn install >> "${log_file}" 2>&1
        printf "\e[36m- Completed to setup the yarn project.: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    elif [ -f "package-lock.json" ]; then
        npm install >> "${log_file}" 2>&1
        printf "\e[36m- Completed to setup the npm project.: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    elif [ -f "package.json" ] && grep -q '"packageManager"[[:space:]]*:[[:space:]]*"pnpm' package.json; then
        pnpm install >> "${log_file}" 2>&1
        printf "\e[36m- Completed to setup the pnpm packageManager project.: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    elif [ -f "package.json" ]; then
        npm install >> "${log_file}" 2>&1
        printf "\e[36m- Completed to setup the npm package project.: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    else
        printf "\e[33m- Skipped to setup Node.js project...: \e[0m\e[36m%s\e[0m\n" "$project_dir"
    fi
    popd >> "${log_file}" 2>&1 || return 1
}

# pnpm workspace 配下のpackageを個別install対象から外す。
# Args:
#   $1: Node.jsプロジェクトディレクトリ。
# Returns:
#   個別セットアップ対象なら 0、workspace root 側で処理すべきなら 1。
function should_setup_nodejs_project() {
    local project_dir=$1

    if [ -f "${project_dir}/pnpm-workspace.yaml" ]; then
        return 0
    fi

    if has_ancestor_file "$project_dir" "pnpm-workspace.yaml"; then
        return 1
    fi

    return 0
}

# Serena MCP サーバのindexを必要な場合だけ作成する。
# Returns:
#   セットアップ成功時は 0、失敗時は非0。
# Side Effects:
#   .serena/cache を作成または更新する。
function setup_serena() {
    chown_dir "${WORKSPACE_ROOT}/.serena"

    if [ ! -d "${WORKSPACE_ROOT}/.serena/cache" ]; then
        pushd "${WORKSPACE_ROOT}" > /dev/null || return 1
        uvx --no-env-file --from git+https://github.com/oraios/serena serena project index
        popd > /dev/null || return 1
        printf "\e[36m- Completed to setup Serena MCP server.\e[0m\n"
    fi
}

# バックグラウンドjobを登録する。
# Args:
#   $@: 実行するコマンド。
# Returns:
#   なし。
# Side Effects:
#   コマンドをバックグラウンド実行し、pidを JOB_PIDS に追加する。
function run_background() {
    "$@" &
    JOB_PIDS+=("$!")
}

# バックグラウンドjobの完了を待ち、失敗を集約する。
# Returns:
#   すべて成功した場合は 0、1つでも失敗した場合は 1。
function wait_for_jobs() {
    local failed=0
    local pid

    for pid in "${JOB_PIDS[@]}"; do
        if ! wait "$pid"; then
            failed=1
        fi
    done

    return "$failed"
}

# devcontainer作成後の依存関係と補助ツールをセットアップする。
# Returns:
#   全セットアップ成功時は 0、失敗時は非0。
# Side Effects:
#   各言語の依存関係、cache所有権、Serena indexを更新する。
function main() {
    printf "\e[34mpostCreateCommand\e[0m\n"

    local script_start
    local script_end
    local total_duration
    local seconds
    local milliseconds
    local project_dir
    declare -g -a JOB_PIDS=()

    script_start=$(date +%s%3N)

    chown_dir "/usr/local/share/nvm"
    chown_dir "${WORKSPACE_ROOT}/.pnpm-store"
    chown_dir "/home/vscode/.cache/uv"
    chown_dir "${WORKSPACE_ROOT}/.serena/cache"

    # Vite+ bin (https://viteplus.dev)
    if [ -f "${HOME}/.vite-plus/env" ]; then
        # shellcheck disable=SC1091
        . "${HOME}/.vite-plus/env"
    fi

    while IFS= read -r project_dir; do
        if should_setup_nodejs_project "$project_dir"; then
            run_background setup_nodejs_project "$project_dir"
        else
            printf "\e[33m- Skipped package under pnpm workspace.: \e[0m\e[36m%s\e[0m\n" "$project_dir"
        fi
    done < <(discover_project_dirs package.json pnpm-lock.yaml pnpm-workspace.yaml yarn.lock package-lock.json)

    while IFS= read -r project_dir; do
        run_background setup_python_project "$project_dir"
    done < <(discover_project_dirs pyproject.toml uv.lock poetry.lock requirements.txt)

    run_background setup_serena

    if ! wait_for_jobs; then
        printf "\e[31mSetup failed. See logs: %s\e[0m\n" "${LOG_DIR}" >&2
        return 1
    fi

    script_end=$(date +%s%3N)
    total_duration=$((script_end - script_start))
    seconds=$((total_duration / 1000))
    milliseconds=$((total_duration % 1000))
    printf "\e[32mSetup complete! Total time: %d.%03d [s]\e[0m\n" "$seconds" "$milliseconds"
}

main
