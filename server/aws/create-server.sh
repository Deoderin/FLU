#!/usr/bin/env bash
# Новий сервер для UGV Quick Setup в AWS. Запускається в AWS CloudShell однією командою з майстра
# (кнопка «Скопіювати команду для AWS»): fleet/aws.py стискає цей файл у base64 і додає аргументи.
#
# Аргументи: назва сервера, регіон, IP ноутбука для SSH (порожньо — лише кнопка Connect у консолі AWS).
# Створює: ключ SSH (файл у домашній теці CloudShell), групу безпеки (WireGuard UDP 51820, SSH для
# Connect і з ноутбука), роль для запасного входу без ключа (Session Manager), сервер Ubuntu 24.04
# t3.micro із зашифрованим диском 8 ГБ і постійну адресу (Elastic IP).
# Далі сервер налаштовує команда з кроку 2 майстра.
set -euo pipefail
NAME=${1:?назва сервера}
REGION=${2:-eu-north-1}
MY_IP=${3:-}
export AWS_DEFAULT_REGION=$REGION AWS_PAGER=""
ERR=/tmp/flu-aws.err

die() { echo; echo "ПОМИЛКА: $*" >&2; exit 1; }

# Назва вже зайнята (лишилося від минулої спроби): нічого не створюємо, щоб не плодити половинчасті сервери
if aws ec2 describe-key-pairs --key-names "$NAME-key" >/dev/null 2>&1 \
  || [ "$(aws ec2 describe-security-groups --filters Name=group-name,Values="$NAME-sg" --query 'length(SecurityGroups)' --output text)" != 0 ]; then
  die "назва $NAME у регіоні $REGION уже зайнята (лишилася від минулої спроби). Змініть назву сервера в майстрі (наприклад, NRK-Server-2) і скопіюйте команду знову"
fi

echo "1/5 Ключ SSH..."
aws ec2 create-key-pair --key-name "$NAME-key" --key-type rsa --key-format pem --query KeyMaterial --output text > "$HOME/$NAME-key.pem"
chmod 600 "$HOME/$NAME-key.pem"

echo "2/5 Правила доступу: WireGuard UDP 51820, SSH для кнопки Connect${MY_IP:+ і з ноутбука $MY_IP}..."
VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
[ "$VPC" != "None" ] || die "у регіоні $REGION немає мережі за замовчуванням (default VPC): створіть її в консолі VPC -> Actions -> Create default VPC"
SG=$(aws ec2 create-security-group --group-name "$NAME-sg" --description "$NAME: WireGuard + SSH" --vpc-id "$VPC" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol udp --port 51820 --cidr 0.0.0.0/0 >/dev/null
# Адреси кнопки Connect: готовий список у регіоні, а де його немає — з офіційного файлу адрес AWS
EIC=$(aws ec2 describe-managed-prefix-lists --filters Name=prefix-list-name,Values="com.amazonaws.$REGION.ec2-instance-connect" --query 'PrefixLists[0].PrefixListId' --output text)
if [ "$EIC" != "None" ]; then
  aws ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,PrefixListIds=[{PrefixListId=$EIC}]" >/dev/null
else
  for CIDR in $(curl -s https://ip-ranges.amazonaws.com/ip-ranges.json | jq -r --arg r "$REGION" '.prefixes[] | select(.service=="EC2_INSTANCE_CONNECT" and .region==$r) | .ip_prefix'); do
    aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 --cidr "$CIDR" >/dev/null
  done
fi
[ -z "$MY_IP" ] || aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 --cidr "$MY_IP/32" >/dev/null

echo "3/5 Запасний вхід без ключа (Session Manager)..."
# Роль і профіль у AWS спільні для всіх регіонів: якщо вони вже є з такою назвою — беремо їх
if ! aws iam get-instance-profile --instance-profile-name "$NAME-ssm" >/dev/null 2>&1; then
  aws iam get-role --role-name "$NAME-ssm" >/dev/null 2>&1 || aws iam create-role --role-name "$NAME-ssm" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$NAME-ssm" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  aws iam create-instance-profile --instance-profile-name "$NAME-ssm" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$NAME-ssm" --role-name "$NAME-ssm"
fi

echo "4/5 Сервер Ubuntu 24.04 (t3.micro, зашифрований диск 8 ГБ)..."
AMI=$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)
ID=""
for i in 1 2 3 4 5 6; do
  sleep 10  # нову роль EC2 бачить не одразу
  if ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type t3.micro --key-name "$NAME-key" --security-group-ids "$SG" \
      --iam-instance-profile Name="$NAME-ssm" \
      --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=8,VolumeType=gp3,Encrypted=true}' \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
      --query 'Instances[0].InstanceId' --output text 2>"$ERR"); then
    break
  fi
  ID=""
done
[ -n "$ID" ] || die "сервер не створився: $(cat "$ERR")"
aws ec2 wait instance-running --instance-ids "$ID"

echo "5/5 Постійна адреса (Elastic IP)..."
ALLOC=$(aws ec2 allocate-address --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME}]" --query AllocationId --output text)
aws ec2 associate-address --instance-id "$ID" --allocation-id "$ALLOC" >/dev/null
IP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].PublicIp' --output text)

echo
echo "ГОТОВО: сервер $NAME ($ID) у $REGION, постійна адреса $IP"
echo "Ключ: Actions -> Download file, у поле шляху вставте повністю (файл буде в «Завантаженнях»):"
echo "  $HOME/$NAME-key.pem"
echo "Коли завантажите, видаліть копію тут: rm ~/$NAME-key.pem"
echo "Далі майстер, крок 2: адреса $IP, користувач ubuntu, «Вибрати файл ключа» -> $NAME-key.pem"
