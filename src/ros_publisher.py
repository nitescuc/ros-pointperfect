import os
import io

import roslibpy

ROS_HOST = os.environ.get('ROS_HOST', 'localhost')
ROS_PORT = int(os.environ.get('ROS_PORT', '9090'))

class RosPointPerfectPublisher:
    def __init__(self):
        self.client = roslibpy.Ros(host=ROS_HOST, port=ROS_PORT)
        self.client.run()
        self.talker = roslibpy.Topic(self.client, '/ntrip_client/rtcm', 'rtcm_msgs/Message')
        self.listener = roslibpy.Topic(self.client, '/nmea', '/nmea_msgs/Sentence')
        self.listener.subscribe(lambda message: self.on_nmea_message(message))
        self.nmea_buffer = None

    def __del__(self):
        self.talker.unadvertise()
        self.listener.unsubscribe()
        self.client.terminate()
    
    def on_nmea_message(self, message):
        self.nmea_buffer = io.BytesIO(bytes(message['sentence'] + "\r\n", 'utf-8'))
    
    def write(self, message):
        message_to_publish = list(message)
        self.talker.publish(roslibpy.Message({'message': message_to_publish}))

    def readinto(self, b):
        if self.nmea_buffer is None:
            return 0
        self.nmea_buffer.seek(0)
        x = self.nmea_buffer.readinto(b)
        self.nmea_buffer = None
        return x
