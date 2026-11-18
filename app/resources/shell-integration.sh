# RPMote Shell Integration — OSC 133 semantic marks + OSC 7 working directory.
#
# These hooks tell the terminal where a prompt starts, when a command begins
# running, where its output starts and what it exited with. That is what makes
# jump-to-prompt, copy-last-output and the exit status in the header exact
# instead of guessed from the screen text.
#
# Two rules govern what can be written here:
#
#   1. Every line is sent to the remote shell on its own. A compound statement
#      spanning lines is fine (the shell reads on until it closes); relying on
#      two statements sharing a line is not.
#   2. This is sent to whatever shell answered, which may be dash, ash or a
#      busybox shell. Those parse the whole file even when a guard is false,
#      so anything they cannot PARSE — array assignment, $'...' — has to sit
#      inside an eval string where only the intended shell ever sees it.

if [ -n "${BASH_VERSION:-}" ]; then
__rpmote_at_prompt=1
__rpmote_preexec() {
if [ -n "${COMP_LINE:-}" ]; then return; fi
if [ "${__rpmote_at_prompt:-0}" != "1" ]; then return; fi
eval 'local __c; for __c in "${PROMPT_COMMAND[@]}"; do case "$__c" in *"$BASH_COMMAND"*) return ;; esac; done'
__rpmote_at_prompt=0
printf '\033]133;C\007'
}
__rpmote_precmd() {
__rpmote_status=$?
printf '\033]133;D;%s\007' "$__rpmote_status"
printf '\033]7;file://%s%s\007' "${HOSTNAME:-}" "$PWD"
__rpmote_at_prompt=1
}
# The DEBUG trap also fires for every command in PROMPT_COMMAND. With a
# distro hook already there (Ubuntu's OSC 3008 emitter, say) it used to fire
# for that hook right after __rpmote_precmd re-armed it, so C landed before
# the prompt and the user's real command got none. Anything that is part of
# PROMPT_COMMAND is therefore skipped, whether it is a string or (bash 5.1+)
# an array; the array syntax stays inside eval so other shells never parse it.
trap '__rpmote_preexec' DEBUG
case "${PROMPT_COMMAND:-}" in
*__rpmote_precmd*) ;;
*) PROMPT_COMMAND="__rpmote_precmd${PROMPT_COMMAND:+; $PROMPT_COMMAND}" ;;
esac
case "${PS1:-}" in
*133*) ;;
*) PS1='\[\e]133;A\a\]'"$PS1"'\[\e]133;B\a\]' ;;
esac
fi

# zsh, kept to one eval'd line so no other shell ever parses its array syntax.
if [ -n "${ZSH_VERSION:-}" ]; then
eval '__rpmote_preexec() { printf "\033]133;C\007"; }; __rpmote_precmd() { __rpmote_status=$?; printf "\033]133;D;%s\007" "$__rpmote_status"; printf "\033]7;file://%s%s\007" "${HOST:-}" "$PWD"; }; case "${precmd_functions[*]:-}" in *__rpmote_precmd*) ;; *) precmd_functions+=(__rpmote_precmd) ;; esac; case "${preexec_functions[*]:-}" in *__rpmote_preexec*) ;; *) preexec_functions+=(__rpmote_preexec) ;; esac; __rpmote_a=$(printf "\033]133;A\007"); __rpmote_b=$(printf "\033]133;B\007"); case "${PS1:-}" in *133*) ;; *) PS1="%{$__rpmote_a%}$PS1%{$__rpmote_b%}" ;; esac'
fi
