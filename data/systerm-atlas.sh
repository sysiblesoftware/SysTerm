# SysTerm — Sysible Atlas shell integration (portable, any distro).
# Sourced by interactive bash. When bash runs inside SysTerm with the Atlas pane
# wired, a per-pane FIFO ($SYSIBLE_ATLAS_FIFO) + id ($SYSIBLE_ATLAS_ID) are
# exported; we write two message kinds to it so a failed command or an
# `ai`/`atlas` question reaches the companion pane. Everything stays local.
if [ -n "$BASH_VERSION" ] && [ -n "$PS1" ] && [ -n "$SYSIBLE_ATLAS_FIFO" ] && [ -p "$SYSIBLE_ATLAS_FIFO" ]; then
    # Hand a failed command to the companion (skip ones that routinely exit
    # non-zero). No typing required — you ask questions in the Atlas pane itself
    # (right-click → Open Sysible Atlas, or Alt+A). Silence with SYSIBLE_ATLAS_AUTO=0.
    __atlas_prompt() {
        local _rc=$?
        [ "${SYSIBLE_ATLAS_AUTO:-1}" = 0 ] && return "$_rc"
        if [ "$_rc" -ne 0 ] && [ "$_rc" -ne 130 ]; then
            local _last _w
            _last=$(HISTTIMEFORMAT= history 1 2>/dev/null | sed 's/^ *[0-9]*[ *]*//')
            _w=${_last%% *}
            case "$_w" in
                grep|egrep|fgrep|rg|ag|test|'['|'[['|diff|cmp|pgrep|pkill|find|ai|atlas|'') return "$_rc" ;;
            esac
            printf 'error\t%s\t%s\t%s\n' "$SYSIBLE_ATLAS_ID" "$_rc" \
                "$(printf '%s' "$_last" | base64 | tr -d '\n')" > "$SYSIBLE_ATLAS_FIFO" 2>/dev/null || true
        fi
        return "$_rc"
    }
    case ";${PROMPT_COMMAND};" in
        *";__atlas_prompt;"*) : ;;
        *) PROMPT_COMMAND="__atlas_prompt;${PROMPT_COMMAND:-}" ;;
    esac
fi
