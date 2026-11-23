# Run a standalone Python script with structured input and arguments
#
# The script receives one NUON document on stdin with `input` and `args`
# fields, and must write one NUON document to stdout.
@example "Multiply pipeline values using double.py" "[1 2 3] | py-call ./double.py 2"
@example "A minimal double.py implementing the interface" r#'
import sys
from nushell_nuon import dumps, loads

request = loads(sys.stdin.read())
factor = request["args"][0]
result = [value * factor for value in request["input"]]
print(dumps(result))
'#
def "py-call" [
    script: path  # Python script to run
    ...args: any  # Typed positional arguments, passed as the `args` list
]: any -> any {
    let python = ($env.PY_CALL_PYTHON? | default "python3")

    {
        input: $in
        args: $args
    }
    | to nuon --raw
    | ^$python $script
    | from nuon
}
