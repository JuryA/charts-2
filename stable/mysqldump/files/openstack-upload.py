#!/usr/bin/python

from optparse import OptionParser
import os, sys, subprocess, json, re, threading

global VERBOSE
VERBOSE = False

class Expando(object):
    pass

def curl(args, shell=True, check=True, input=None, timeout_sec = 30, **kwargs):
    '''python3 subprocess.run() workalike with more appropriate defaults '''

    args = f'curl {args}'
    if VERBOSE:
        print(f"Run:\n  {args}")
    p = subprocess.Popen(args, shell=shell, stdin=subprocess.PIPE if input else None, stdout=subprocess.PIPE, **kwargs)

    timer = threading.Timer(timeout_sec, p.kill)
    try:
        timer.start()
        (stdout, stderr) = p.communicate(input.encode('utf-8') if input else '')
    finally:
        timer.cancel()

    r = Expando()
    r.stdout = stdout
    r.stderr = stderr or ''
    r.returncode = p.returncode

    if VERBOSE:
        print(f"Result:\n{r.stdout}".replace('\n', '\n  '))

    if check and r.returncode != 0:
        raise Exception(f"Command {args} exited with code {r.returncode}:\n{r.stdout}")

    return r

def upload(source, destination, ttl_days):
    try:
        auth_url = os.environ['OS_AUTH_URL'] + '/auth/tokens'
        print(f'Getting authentication token from {auth_url} ...')

        auth_json = '''{{
            "auth": {{
                "identity": {{
                    "methods": ["password"],
                    "password": {{
                        "user": {{
                            "domain": {{"name": "{OS_USER_DOMAIN_NAME}"}},
                            "name": "{OS_USERNAME}",
                            "password": "{OS_PASSWORD}"
                        }}
                    }}
                }},
                "scope": {{
                    "project": {{
                        "domain": {{"name": "{OS_PROJECT_DOMAIN_NAME}"}},
                        "name": "{OS_PROJECT_NAME}"
                    }}
                }}
            }}
        }}'''.format(**dict({k:v.strip() for (k,v) in os.environ.iteritems() if k.startswith("OS_")}))
    except KeyError as e:
        raise Exception('These environment variables must be set with login info:\n'+
              '  OS_USER_DOMAIN_NAME, OS_USERNAME, OS_PASSWORD, OS_PROJECT_DOMAIN_NAME, OS_PROJECT_NAME, OS_AUTH_URL', e)

    if VERBOSE:
        print("Login data:\n  " + auth_json)

    # check that it is valid
    json.loads(auth_json)

    p = curl(
        f'--silent --show-error --include --header "Content-Type: application/json" --data @- {auth_url}',
        input=auth_json.encode('utf-8'),
    )

    lines = [s.decode('utf-8') for s in p.stdout.splitlines()]
    header_lines = lines[:-2]
    body = lines[-1]

    if p.returncode:
        raise Exception(f"Failed to log in to Openstack at {auth_url}: exit code {p}")

    if header_lines[0].split()[1] != '201':
        raise Exception(
            f"Failed to log in to Openstack at {auth_url}: {header_lines[0]}\n{body}"
        )

    for line in header_lines:
        if line.startswith('X-Subject-Token:'):
            auth_token = line.split()[1]
            break
    else:
        raise Exception("Failed to find X-Subject-Token in returned headers:\n  {}".format("\n  ".join(header_lines)))

    auth_token_header = 'X-Auth-Token: {0}\nX-Storage-Token: {0}'.format(auth_token)
    if VERBOSE:
        print(f"\nHeaders:\n  {auth_token_header}\n")

    print('Locating public object store URL in catalog ...')
    object_store_url = None
    for i in json.loads(body)["token"]["catalog"]:
        if i["type"] != "object-store":
            continue
        for j in i["endpoints"]:
            if j["interface"] == "public":
                object_store_url = j["url"]
                break
        else:
            continue
        break

    if object_store_url is None:
        raise Exception(
            f"Failed to find object-store public endpoint URL in returned JSON:\n{body}"
        )

    print(f"  {object_store_url}")

    full_destination_url = f'{object_store_url}/{destination}'

    # Check container
    container_name = destination.split('/')[0]
    container_url = f"{object_store_url}/{container_name}"
    print(f'Checking container: {container_name} ...')
    p = curl(
        f'--fail --silent --show-error --head --header @- {container_url}',
        input=auth_token_header,
    )
    if not VERBOSE:
        print(f"  {p.stdout.splitlines()[0]}")

    # Upload
    print(
        f'\nUploading\n  from: {os.path.join(os.getcwd(), source)}\n  to:   {full_destination_url}'
    )
    if ttl_days:
        ttl_seconds = ttl_days * 24 * 60 * 60
        print(f'  ttl:  {ttl_days} day(s)\n')
    else:
        print('  ttl:  None\n')

    found = 0
    count = 0
    errs = 0
    for path, _, files in os.walk(source):
        abspath = os.path.join(os.getcwd(), path)
        if VERBOSE:
            print(f"\nEntering '{abspath}'")

        for f in files:
            found += 1

            local_file = os.path.join(abspath, f)
            dest_url = f'{full_destination_url}/{path}/{f}'.replace('//', '/')

            if VERBOSE:
                print(f"\n{local_file} ==> {dest_url}\n")
            else:
                print(f'  {local_file}')

            try:
                p = curl('--silent --show-error --head --header @- -o /dev/null --write-out "%{{http_code}}" {}'.format(dest_url),
                    input=auth_token_header)

                if p.stdout.strip() != '404':
                    if VERBOSE: print("  File found on server, not uploading.")
                    continue # Don't upload existing file, or on error

                print("    Uploading ...")

                upload_args = f'--silent --show-error --fail --request PUT --header @- --upload-file {local_file} {dest_url}'
                if ttl_days:
                    upload_args += f' --header "X-Delete-After: {ttl_seconds}"'

                p = curl(upload_args, shell=True, check=True, timeout_sec=30*60, input=auth_token_header)

                count += 1
            except Exception as e:
                print(f"ERROR: {e}")
                errs += 1

    print(f'\nUploaded {count} of {found} file(s), {errs} failure(s)')

    if errs:
        print("FAIL: Error(s) occured")
        return 2

    if not count and found:
        print("WARN: No files uploaded?")
        return 1

    if not found:
        print("WARN: No files found?")
        return 1

    return 0

if __name__ == '__main__':
    print('This script uploads files to an OpenStack object store container\n')
    # Inspired by http://doc.swift.surfsara.nl/en/latest/Pages/Clients/curl_token.html#curl-token

    parser = OptionParser()
    parser.add_option("-s", "--source", action="store", type="string", dest="source",
                      help="Source directory to copy from")
    parser.add_option("-d", "--destination", action="store", type="string", dest="destination",
                      help="Destination URL to copy to, starting with container name")
    parser.add_option("-t", "--ttl-days", action="store", type="int", dest="ttl_days", default=0,
                      help="Days to set TTL/Delete-After, 0 to disable")
    parser.add_option("-v", "--verbose", action="store_true", dest="verbose", default=False,
                      help="Verbose logging output")

    (options, args) = parser.parse_args()

    for r in ['source', 'destination']:
        if not options.__dict__.get(r):
            parser.error(f"parameter {r} required")


    VERBOSE = options.verbose

    r = upload(source=options.source, destination=options.destination, ttl_days=options.ttl_days)
    sys.exit(r)
