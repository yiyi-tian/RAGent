#include "executor/policy.h"
#include "protocol/protocol.h"

#include <stdio.h>
#include <string.h>

int main(void)
{
    unsigned char input[131072];
    size_t length = fread(input, 1, sizeof(input), stdin);
    json_object *request = length < sizeof(input) ? json_parse_object(input, length) : NULL;
    json_object *value = NULL;
    if (request == NULL || !json_object_object_get_ex(request, "command", &value)
        || !json_object_is_type(value, json_type_string)
        || strlen(json_object_get_string(value)) != (size_t)json_object_get_string_len(value)) {
        if (request != NULL) {
            json_object_put(request);
        }
        return 1;
    }
    json_object *result = command_policy(json_object_get_string(value));
    int status = puts(json_object_to_json_string_ext(result, JSON_C_TO_STRING_PLAIN)) < 0;
    json_object_put(result);
    json_object_put(request);
    return status;
}
