#include "executor/policy.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(void)
{
    struct {
        const char *command;
        int level;
    } cases[] = {
        { "ls", 1 },
        { "pwd", 1 },
        { "find . -maxdepth 1 -type f | wc -l", 1 },
        { "cat README.md", 1 },
        { "head -n 4 README.md", 1 },
        { "tail -n 2 README.md", 1 },
        { "grep -n 'hello world' README.md", 1 },
        { "find . -type f -name '*.c'", 1 },
        { "git status --short", 1 },
        { "git log --oneline -n 5", 1 },
        { "git diff --stat", 1 },
        { "ls && (pwd | cat)", 1 },
        { "ls || pwd; cat README.md", 1 },
        { "ls; touch file", 2 },
        { "touch a && ls", 2 },
        { "mkdir -p notes", 2 },
        { "rm -rf notes", 2 },
        { "cp a b | cat", 2 },
        { "mv a b", 2 },
        { "sed -i 's/old/new/g' file", 2 },
        { "echo hello > file", 2 },
        { "echo hello >> file", 2 },
        { "find . -delete", 2 },
        { "ls; curl https://example.com", 3 },
        { "wget https://example.com", 3 },
        { "pip install package", 3 },
        { "npm install", 3 },
        { "git push", 3 },
        { "ssh server", 3 },
        { "scp file server:file", 3 },
        { "bash -c 'ls'", 3 },
        { "python -c 'print(1)'", 3 },
        { "perl -e 'print 1'", 3 },
        { "eval 'ls'", 3 },
        { "echo $(touch marker)", 3 },
        { "cat <(curl https://example.com)", 3 },
        { "ls `curl example.com`", 3 },
        { "find . -exec sh -c 'touch x' ';'", 3 },
        { "find . -fprint result", 2 },
        { "find . -fprint0 result", 2 },
        { "find . -fprintf result '%p'", 2 },
        { "find . -fls result", 2 },
        { "grep --exclude-from=.env token .", 3 },
        { "git -c core.pager=sh log", 3 },
        { "git log --output=marker", 3 },
        { "git diff --ext-diff", 3 },
        { "cat ~/.ssh/id_rsa", 3 },
        { "cat .env", 3 },
        { "cat credentials.pem", 3 },
        { "sed -i 's/x/id/e' file", 3 },
        { "sed -i 's/x/y/w other' file", 3 },
        { "sed -i 's/x/y/;e touch marker' file", 3 },
        { "./ls", 3 },
        { "PATH=/tmp ls", 3 },
        { "$COMMAND", 3 },
        { "ls &", 3 },
        { "while true; do ls; done", 3 },
        { "ls; rm file; curl example.com", 3 },
    };
    for (size_t index = 0; index < sizeof(cases) / sizeof(cases[0]); ++index) {
        json_object *result = command_policy(cases[index].command);
        int level = json_object_get_int(json_object_object_get(result, "level"));
        if (level != cases[index].level) {
            fprintf(stderr,
                "%s: expected=%d actual=%s\n",
                cases[index].command,
                cases[index].level,
                json_object_to_json_string(result));
        }
        assert(level == cases[index].level);
        assert(json_object_get_boolean(json_object_object_get(result, "requires_confirmation"))
            == (level != 1));
        assert(strcmp(json_object_get_string(json_object_object_get(result, "environment")),
                   level == 1 ? "host_readonly" : "container")
            == 0);
        json_object_put(result);
    }
    const char *invalid[] = { "", "ls &&", "echo 'unfinished", "# comment only" };
    for (size_t index = 0; index < sizeof(invalid) / sizeof(invalid[0]); ++index) {
        json_object *result = command_policy(invalid[index]);
        assert(json_object_get_int(json_object_object_get(result, "level")) == 3);
        assert(
            json_object_is_type(json_object_object_get(result, "denial_reason"), json_type_string));
        json_object_put(result);
    }
    return 0;
}
