FROM ros:noetic-ros-base-focal

RUN apt update && apt install -y python3-pip

WORKDIR /usr/src
ADD src/requirements.txt /usr/src

RUN pip3 install -r requirements.txt

ADD src /usr/src

CMD ["python3", "main.py", "-j /config/config.json", "-l"]
