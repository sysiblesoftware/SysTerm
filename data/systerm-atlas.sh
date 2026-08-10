# SysTerm — Sysible Atlas shell integration (portable, any distro).
# Sourced by interactive bash. When bash runs inside SysTerm with the Atlas pane
# wired, a per-pane FIFO ($SYSIBLE_ATLAS_FIFO) + id ($SYSIBLE_ATLAS_ID) are
# exported; we write a failed command to it so the companion pane can explain it.
# Everything stays local.
if [ -n "$BASH_VERSION" ] && [ -n "$PS1" ] && [ -n "$SYSIBLE_ATLAS_FIFO" ] && [ -p "$SYSIBLE_ATLAS_FIFO" ]; then
    # Capture the command AS IT RUNS, not from `history` afterwards. The DEBUG
    # trap fires before every command; we record only TOP-LEVEL commands (the ones
    # you actually type), so a git-in-your-prompt helper or a PROMPT_COMMAND
    # function can't be mistaken for your command. PS1 $(...) substitutions run in
    # subshells that don't inherit DEBUG, so they're never captured either. Your
    # real command always overwrites any prompt noise just before it executes, so
    # by the time the prompt hook reads it, it's correct.
    __atlas_cmd=""
    __atlas_debug() {
        [ -n "$COMP_LINE" ] && return              # ignore tab-completion
        [ "${#FUNCNAME[@]}" -le 1 ] || return      # only top-level commands
        case "$BASH_COMMAND" in
            __atlas_*|__sysible_*) return ;;       # our own hooks
        esac
        __atlas_cmd=$BASH_COMMAND
    }
    trap '__atlas_debug' DEBUG

    # After a failed command, hand it to the companion (skip commands that
    # routinely exit non-zero). Silence with SYSIBLE_ATLAS_AUTO=0.
    __atlas_prompt() {
        local _rc=$?
        # Skip the FIRST prompt of each shell: it fires right after bashrc/profile
        # sourcing, whose leftover $? and captured command would surface as a
        # phantom "zombie" catch before you've run anything.
        if [ -z "$__atlas_ready" ]; then
            __atlas_ready=1
            __atlas_cmd=""
            return "$_rc"
        fi
        if [ "${SYSIBLE_ATLAS_AUTO:-1}" != 0 ] && [ "$_rc" -ne 0 ] \
           && [ "$_rc" -ne 130 ] && [ -n "$__atlas_cmd" ]; then
            local _w=${__atlas_cmd%% *}
            case "$_w" in
                grep|egrep|fgrep|rg|ag|test|'['|'[['|diff|cmp|pgrep|pkill|find|ai|atlas|'') : ;;
                *)
                    printf 'error\t%s\t%s\t%s\n' "$SYSIBLE_ATLAS_ID" "$_rc" \
                        "$(printf '%s' "$__atlas_cmd" | base64 | tr -d '\n')" \
                        > "$SYSIBLE_ATLAS_FIFO" 2>/dev/null || true
                    ;;
            esac
        fi
        __atlas_cmd=""
        return "$_rc"
    }
    case ";${PROMPT_COMMAND};" in
        *";__atlas_prompt;"*) : ;;
        *) PROMPT_COMMAND="__atlas_prompt;${PROMPT_COMMAND:-}" ;;
    esac
fi
