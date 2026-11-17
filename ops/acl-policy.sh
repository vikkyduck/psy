# Named ACLs of the directories several service accounts share. The one policy: prepare-host
# applies it to new directories, restore-permissions to recovered ones. Source this file.
#
# For each directory: the ACL of the directory itself, the ACL that entries already inside get
# (the same effective rights a new entry would inherit), and the default ACL new entries inherit.
#   queue    /home/hermes/.hermes/vault/uploads         uploader and sender read and write
#   media    /home/hermes/.hermes/media-vault/uploads   uploader writes recordings, sender writes
#            results; ASR may only open files it is given (traverse, no listing) and read them
#   progress /var/lib/psy-asr/progress                  ASR writes, the uploader only reads

PSY_ACL_QUEUE=(u:psy-upload:rwx,u:psy-deliver:rwx
               u:psy-upload:rwX,u:psy-deliver:rwX
               d:u:hermes:rwx,d:u:psy-upload:rwx,d:u:psy-deliver:rwx,d:g::---,d:o::---)
PSY_ACL_MEDIA=(u:psy-upload:rwx,u:psy-deliver:rwx,u:psy-asr:--x
               u:psy-upload:rwX,u:psy-deliver:rX,u:psy-asr:rX
               d:u:hermes:rwx,d:u:psy-upload:rwx,d:u:psy-deliver:rx,d:u:psy-asr:rx,d:g::---,d:o::---)
PSY_ACL_PROGRESS=(u:psy-upload:rx
                  u:psy-upload:rX
                  d:u:psy-upload:rx,d:g::rx,d:o::---)

# psy_shared_acl DIRECTORY QUEUE|MEDIA|PROGRESS [COMMAND PREFIX...]
#   the directory's own ACL and the default ACL. The prefix runs setfacl, e.g. as the owner:
#   psy_shared_acl "$q" QUEUE runuser -u hermes --
# psy_shared_acl_entries DIRECTORY QUEUE|MEDIA|PROGRESS [COMMAND PREFIX...]
#   everything already inside (recovered files), as if created under the default ACL.
# Links are never followed: callers refuse them, and find skips them here.
psy_acl_policy() {
    local -n chosen=PSY_ACL_$1
    [[ ${#chosen[@]} == 3 ]] || { echo "No ACL policy named $1" >&2; return 1; }
    policy=("${chosen[@]}")
}
psy_shared_acl() {
    local dir=$1 policy
    psy_acl_policy "$2" || return 1
    shift 2
    [[ -d $dir && ! -L $dir ]] || { echo "Not a directory: $dir" >&2; return 1; }
    "$@" setfacl -m "${policy[0]},${policy[2]}" "$dir"
}
psy_shared_acl_entries() {
    local dir=$1 policy
    psy_acl_policy "$2" || return 1
    shift 2
    [[ -d $dir && ! -L $dir ]] || { echo "Not a directory: $dir" >&2; return 1; }
    find "$dir" -mindepth 1 ! -type l -exec "$@" setfacl -m "${policy[1]}" {} +
    find "$dir" -mindepth 1 -type d -exec "$@" setfacl -m "${policy[2]}" {} +
}
