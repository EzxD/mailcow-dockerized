#!/usr/bin/env bash
[[ -f mailcow.conf ]] && source mailcow.conf
[[ -f ../mailcow.conf ]] && source ../mailcow.conf

if [[ -z ${DBUSER} ]] || [[ -z ${DBPASS} ]] || [[ -z ${DBNAME} ]]; then
	echo "Cannot find mailcow.conf, make sure this script is run from within the mailcow folder."
	exit 1
fi

SKIP_CONFIRM=false
if [[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]]; then
    SKIP_CONFIRM=true
    shift # keep the optional password length in $1
fi

echo -n "Checking MySQL service... "
if ! mysql_container=$(docker ps -qf name=mysql-mailcow) || [[ -z "${mysql_container}" ]]; then
	echo "failed"
	echo "MySQL (mysql-mailcow) is not up and running, exiting..."
	exit 1
fi

echo "OK"
if [[ "$SKIP_CONFIRM" == "true" ]]; then
    response="yes"
else
    read -r -p "Are you sure you want to reset the mailcow administrator account? [y/N] " response
    response=${response,,}
fi
if [[ "$response" =~ ^(yes|y)$ ]]; then
	echo -e "\nWorking, please wait..."
  if ! dovecot_container=$(docker ps -qf name=dovecot-mailcow) || [[ -z "${dovecot_container}" ]]; then
    echo "Dovecot (dovecot-mailcow) is not up and running, exiting..." >&2
    exit 1
  fi

  password_length=${1:-16}
  if [[ ! "${password_length}" =~ ^[1-9][0-9]*$ ]]; then
    echo "Password length must be a positive integer." >&2
    exit 1
  fi
  # head closes the pipe once enough bytes arrive, so tr may exit with SIGPIPE
  # (141). Check both commands and the length to reject other errors/short reads.
  if ! random=$(
    LC_ALL=C tr -dc 'A-Za-z0-9_-' < /dev/urandom | head -c "${password_length}"
    status=("${PIPESTATUS[@]}")
    [[ ${status[0]} == 0 || ${status[0]} == 141 ]] && [[ ${status[1]} == 0 ]]
  ) || [[ ${#random} != "${password_length}" ]]; then
    echo "Failed to generate a random password, exiting..." >&2
    exit 1
  fi
  if ! password=$(docker exec "${dovecot_container}" doveadm pw -s SSHA256 -p "${random}") || [[ -z "${password}" ]]; then
    echo "Failed to generate password hash, exiting..." >&2
    exit 1
  fi

  # Keep all changes in one transaction. mysql -e stops on error, and closing
  # the connection rolls back any changes that have not been committed.
  if ! docker exec "${mysql_container}" mysql -u"${DBUSER}" -p"${DBPASS}" "${DBNAME}" -e "
    START TRANSACTION;
    DELETE FROM admin WHERE username='admin';
    DELETE FROM domain_admins WHERE username='admin';
    INSERT INTO admin (username, password, superadmin, active) VALUES ('admin', '${password}', 1, 1);
    DELETE FROM tfa WHERE username='admin';
    COMMIT;"; then
    echo "Failed to reset administrator account, exiting..." >&2
    exit 1
  fi
	echo "
Reset credentials:
---
Username: admin
Password: ${random}
TFA: none
"
else
	echo "Operation canceled."
fi
