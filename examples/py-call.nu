# Run a standalone Python script with structured input and parameters
#
# The script receives one NUON document on stdin with `input` and `params`
# fields, and must write one NUON document to stdout.
@example "Multiply pipeline values using double.py" "[1 2 3] | py-call ./double.py {factor: 2}"
@example "A minimal double.py implementing the interface" r#'
import sys
import nuon

request = nuon.loads(sys.stdin.read())
factor = request["params"]["factor"]
result = [value * factor for value in request["input"]]
print(nuon.dumps(result))
'#
def "py-call" [
    script: path  # Python script to run
    params?: any  # Optional structured parameters; null when omitted
]: any -> any {
    let python = ($env.PY_CALL_PYTHON? | default "python3")

    {
        input: $in
        params: $params
    }
    | to nuon --raw
    | ^$python $script
    | from nuon
}
