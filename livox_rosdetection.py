import atexit
import csv
import pickle
import os
import numpy as np
import tensorflow as tf
import copy
import config.config as cfg
from networks.model import *
import lib_cpp
import math
import socket
import sys
import time

import rospy
import std_msgs.msg

from datetime import datetime
from geometry_msgs.msg import Point
from sensor_msgs.msg import PointCloud2
from geometry_msgs.msg import Point32
from geometry_msgs.msg import Quaternion
import sensor_msgs.point_cloud2 as pcl2
from visualization_msgs.msg import Marker
from visualization_msgs.msg import MarkerArray

DEBUG=1

#check for input
lidar_height = 0
HEIGHT_CORRECTION = -1.5
n = len(sys.argv)
try:
    lidar_height = float(sys.argv[1]) + HEIGHT_CORRECTION
    
    folder_num = int(sys.argv[2])
except:
    print("Not enough args")
    sys.exit()

#multimem shit
#a = np.array([0])
mnum = 0
marker_array = MarkerArray()
marker_array_text = MarkerArray()

DX = cfg.VOXEL_SIZE[0]
DY = cfg.VOXEL_SIZE[1]
DZ = cfg.VOXEL_SIZE[2]

X_MIN = cfg.RANGE['X_MIN']
X_MAX = cfg.RANGE['X_MAX']

Y_MIN = cfg.RANGE['Y_MIN']
Y_MAX = cfg.RANGE['Y_MAX']

Z_MIN = cfg.RANGE['Z_MIN']
Z_MAX = cfg.RANGE['Z_MAX']

overlap = cfg.OVERLAP
HEIGHT = round((X_MAX - X_MIN+2*overlap) / DX)
WIDTH = round((Y_MAX - Y_MIN) / DY)
CHANNELS = round((Z_MAX - Z_MIN) / DZ)



print(HEIGHT, WIDTH, CHANNELS)

T1 = np.array([[0.0, -1.0, 0.0, 0.0],
               [0.0, 0.0, -1.0, 0.0],
               [1.0, 0.0, 0.0, 0.0],
               [0.0, 0.0, 0.0, 1.0]]
              )
lines = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6],
         [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]]

def debug(line):
  if(DEBUG):
    print(line)

class Detector(object):
    def __init__(self, *, nms_threshold=0.1, weight_file=None, port=1028):
        self.folder_name = 'Flight_Logs'
        self.folder_num = folder_num
        self.edge_file = None
        self.csv_writer = None
        self.init_file()    

        self.net = livox_model(HEIGHT, WIDTH, CHANNELS)
        with tf.Graph().as_default():
            with tf.device('/gpu:'+str(cfg.GPU_INDEX)):
                input_bev_img_pl = \
                    self.net.placeholder_inputs(cfg.BATCH_SIZE)
                end_points = self.net.get_model(input_bev_img_pl)

                saver = tf.train.Saver()
                config = tf.ConfigProto()
                config.gpu_options.allow_growth = True
                config.allow_soft_placement = True
                config.log_device_placement = False
                self.sess = tf.Session(config=config)
                saver.restore(self.sess, cfg.MODEL_PATH)
                self.ops = {'input_bev_img_pl': input_bev_img_pl,  # input
                            'end_points': end_points,  # output
                            }
        self.ped_count = 0
        #rospy.init_node('livox_test', anonymous=True)
        
        #/livox/odin_frame /livox/lidar
        # self.sub = rospy.Subscriber(
        #     "/livox/odin_frame", PointCloud2, queue_size=1, buff_size=2**24, callback=self.LivoxCallback)
        self.marker_pub = rospy.Publisher(
            '/detect_box3d', MarkerArray, queue_size=10)
        self.marker_text_pub = rospy.Publisher(
            '/text_det', MarkerArray, queue_size=10)
        self.pointcloud_pub = rospy.Publisher(
            '/pointcloud', PointCloud2, queue_size=10)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.port = port
        self.sock.bind(('', self.port))
        self.sock.listen(1)
        debug("Listening for clients")
        
    def init_file(self):
        path = os.getcwd()
        path = path + '/' + self.folder_name
        if not os.path.exists(path):
            os.mkdir(path)

        path = path + '/' + 'Flight_Logs' + str(self.folder_num)
        if not os.path.exists(path):
            os.mkdir(path)

        file_name = datetime.now().strftime("%m_%d_%Y_%H-%M-%S") + '_edge_log.csv'
        self.edge_file = open(path + '/' + file_name, "a+")
        self.csv_writer = csv.writer(self.edge_file)
        self.csv_writer.writerow(['seq', 'det_time', 'ped_count', 'num_detections', 'detections'])

    def register_exit_function(self):
        atexit.register(self.close_file_and_exit)

    def close_file_and_exit(self):
        print("closing file")
        self.edge_file.close()
        sys.exit()

    def recv_end(self, socket) -> 'byte stream':
        '''
        Taken from https://code.activestate.com/recipes/408859/
        '''
        total_data = []
        while True:
            data = socket.recv(500000)
            #print("len data: ", len(data))
            if not data:
                debug("No data")
                sys.exit()
            if bEND in data:
                #debug("bEND found in data")
                total_data.append(data[:data.find(bEND)])
                break
            total_data.append(data)
            #debug("len of adding total_data array: " + str(len(total_data)))
            if len(total_data) > 1:
                #check if the data was split between last two "data packets"
                last_pair = total_data[-2] + total_data[-1]
                if bEND in last_pair:
                    #debug("popping")
                    total_data[-2] = last_pair[:last_pair.find(bEND)]
                    total_data.pop()
                    break
        #debug("len of total_data array: " + str(len(total_data)))

        # find frame data and altitude, return as tuple
        message = b''.join(total_data)
        
        divider = message.find(bINTER)
        end = message.find(bEND)
        frame = pickle.loads(data[:divider])
        altitude = pickle.loads(message[divider+len(bINTER):end])

        return frame, altitude
        #return b''.join(total_data)
    
    def main_func(self):
        new_sock, addr = self.sock.accept()
        debug("Established connection")

        while True:
            try:
                frame, altitude = self.recv_end(new_sock)

                self.LivoxCallback(frame, altitude)

                # Send the acknowledgement after detection is done
                ack_data = pickle.dumps("ack")
                new_sock.send(ack_data)

            except Exception as e:
                traceback.print_exc()
                print(e)
                sys.exit()
                
        

    def roty(self, t):
        c = np.cos(t)
        s = np.sin(t)
        return np.array([[c,  0,  s],
                         [0,  1,  0],
                         [-s, 0,  c]])

    def get_3d_box(self, box_size, heading_angle, center):
        ''' Calculate 3D bounding box corners from its parameterization.

        Input:
            box_size: tuple of (l,w,h)
            heading_angle: rad scalar, clockwise from pos x axis
            center: tuple of (x,y,z)
        Output:
            corners_3d: numpy array of shape (8,3) for 3D box cornders
        '''
        R = self.roty(heading_angle)
        l, w, h = box_size
        x_corners = [l/2, l/2, -l/2, -l/2, l/2, l/2, -l/2, -l/2]
        y_corners = [h/2, h/2, h/2, h/2, -h/2, -h/2, -h/2, -h/2]
        z_corners = [w/2, -w/2, -w/2, w/2, w/2, -w/2, -w/2, w/2]
        corners_3d = np.dot(R, np.vstack([x_corners, y_corners, z_corners]))
        corners_3d[0, :] = corners_3d[0, :] + center[0]
        corners_3d[1, :] = corners_3d[1, :] + center[1]
        corners_3d[2, :] = corners_3d[2, :] + center[2]
        corners_3d = np.transpose(corners_3d)
        return corners_3d

    def data2voxel(self, pclist):

        data = [i*0 for i in range(HEIGHT*WIDTH*CHANNELS)]

        for line in pclist:
            X = float(line[0])
            Y = float(line[1])
            Z = float(line[2])
            if( Y > Y_MIN and Y < Y_MAX and
                X > X_MIN and X < X_MAX and
                Z > Z_MIN and Z < Z_MAX):
                channel = int((-Z + Z_MAX)/DZ)
                if abs(X)<3 and abs(Y)<3:
                    continue
                if (X > -overlap):
                    pixel_x = int((X - X_MIN + 2*overlap)/DX)
                    pixel_y = int((-Y + Y_MAX)/DY)
                    data[pixel_x*WIDTH*CHANNELS+pixel_y*CHANNELS+channel] = 1
                if (X < overlap):
                    pixel_x = int((-X + overlap)/DX)
                    pixel_y = int((Y + Y_MAX)/DY)
                    data[pixel_x*WIDTH*CHANNELS+pixel_y*CHANNELS+channel] = 1
        voxel = np.reshape(data, (HEIGHT, WIDTH, CHANNELS))
        return voxel

    def detect(self, batch_bev_img):
        feed_dict = {self.ops['input_bev_img_pl']: batch_bev_img}
        feature_out,\
            = self.sess.run([self.ops['end_points']['feature_out'],
                             ], feed_dict=feed_dict)
        result = lib_cpp.cal_result(feature_out[0,:,:,:], \
                                    cfg.BOX_THRESHOLD,overlap,X_MIN,HEIGHT, WIDTH, cfg.VOXEL_SIZE[0], cfg.VOXEL_SIZE[1], cfg.VOXEL_SIZE[2], cfg.NMS_THRESHOLD)
        is_obj_list = result[:, 0].tolist()
        
        reg_m_x_list = result[:, 5].tolist()
        reg_w_list = result[:, 4].tolist()
        reg_l_list = result[:, 3].tolist()
        obj_cls_list = result[:, 1].tolist()
        reg_m_y_list = result[:, 6].tolist()
        reg_theta_list = result[:, 2].tolist()
        reg_m_z_list = result[:, 8].tolist()
        reg_h_list = result[:, 7].tolist()
        
        results = []
        self.ped_count = 0
        for i in range(len(is_obj_list)):
            box3d_pts_3d = np.ones((8, 4), float)
            box3d_pts_3d[:, 0:3] = self.get_3d_box( \
                (reg_l_list[i], reg_w_list[i], reg_h_list[i]), \
                reg_theta_list[i], (reg_m_x_list[i], reg_m_z_list[i], reg_m_y_list[i]))
            box3d_pts_3d = np.dot(np.linalg.inv(T1), box3d_pts_3d.T).T  # n*4
            if int(obj_cls_list[i]) == 0:
                cls_name = "car"
            elif int(obj_cls_list[i]) == 1:
                cls_name = "bus"
            elif int(obj_cls_list[i]) == 2:
                cls_name = "truck"
            elif int(obj_cls_list[i]) == 3:
                cls_name = "pedestrian"
                self.ped_count += 1
            else:
                cls_name = "bimo"
            results.append([cls_name,
                            box3d_pts_3d[0][0], box3d_pts_3d[1][0], box3d_pts_3d[2][0], box3d_pts_3d[3][0],
                            box3d_pts_3d[4][0], box3d_pts_3d[5][0], box3d_pts_3d[6][0], box3d_pts_3d[7][0],
                            box3d_pts_3d[0][1], box3d_pts_3d[1][1], box3d_pts_3d[2][1], box3d_pts_3d[3][1],
                            box3d_pts_3d[4][1], box3d_pts_3d[5][1], box3d_pts_3d[6][1], box3d_pts_3d[7][1],
                            box3d_pts_3d[0][2], box3d_pts_3d[1][2], box3d_pts_3d[2][2], box3d_pts_3d[3][2],
                            box3d_pts_3d[4][2], box3d_pts_3d[5][2], box3d_pts_3d[6][2], box3d_pts_3d[7][2],
                            is_obj_list[i]])
        return results

    def LivoxCallback(self, msg, altitude):
        global mnum
        t0 = time.time()
        header = std_msgs.msg.Header()
        header.stamp = rospy.Time.now()
        header.frame_id = 'livox_frame'
        points_list = []
        t2 = time.time()
        for point in pcl2.read_points(msg, skip_nans=True, field_names=("x", "y", "z", "intensity")):
            if point[0] == 0 and point[1] == 0 and point[2] == 0:
                continue
            if np.abs(point[0]) < 2.0 and np.abs(point[1]) < 1.5:
                continue

            #45 degrees slant
            x = point[0]*math.cos(math.radians(44))+point[2]*math.sin(math.radians(44))
            y = point[1]
            z = point[2]*math.cos(math.radians(44))-point[0]*math.sin(math.radians(44)) + altitude - 0.358775 # height difference between LiDAR and gps
            #formula is height - 1.5, since ground level is at -1.9m and you subtract -0.4m sxince LiDAR is a little bit below the actual height.
            new_pt = (x, y, z, point[3])
            points_list.append(new_pt)


            #code below is for vertical
            # new_pt = (point[0], point[1], point[2]-1.9, point[3])
            # points_list.append(new_pt)
            
            #original
            #points_list.append(point)
        t3 = time.time()
        print('proc_time(ms)', 1000*(t3-t2))
        points_list = np.asarray(points_list)
        pointcloud_msg = pcl2.create_cloud_xyz32(header, points_list[:, 0:3])
        vox = self.data2voxel(points_list)
        vox = np.expand_dims(vox, axis=0)
        
        result = self.detect(vox)
        
        
        print('det_numbers', len(result))
        for ii in range(len(result)):
            result[ii][1:9] = list(np.array(result[ii][1:9]))
            result[ii][9:17] = list(np.array(result[ii][9:17]))
            result[ii][17:25] = list(np.array(result[ii][17:25]))
        boxes = result
        marker_array.markers.clear()
        marker_array_text.markers.clear()

        csv_marker_array = []
        for obid in range(len(boxes)):
            ob = boxes[obid]
            tid = 0
            detect_points_set = []
            for i in range(0, 8):
                detect_points_set.append(Point(ob[i+1], ob[i+9], ob[i+17]))

            marker = Marker()
            marker.header.frame_id = 'livox_frame'
            marker.header.stamp = rospy.Time.now()

            marker.id = obid*2
            marker.action = Marker.ADD
            marker.type = Marker.LINE_LIST

            marker.lifetime = rospy.Duration(0)

            marker.color.r = 1
            marker.color.g = 0
            marker.color.b = 0

            marker.color.a = 1
            marker.scale.x = 0.2
            marker.points = []

            for line in lines:
                marker.points.append(detect_points_set[line[0]])
                marker.points.append(detect_points_set[line[1]])

            marker_array.markers.append(marker)
            marker1 = Marker()
            marker1.header.frame_id = 'livox_frame'
            marker1.header.stamp = rospy.Time.now()
            marker1.ns = "basic_shapes"

            marker1.id = obid*2+1
            marker1.action = Marker.ADD

            marker1.type = Marker.TEXT_VIEW_FACING

            marker1.lifetime = rospy.Duration(0)

            marker1.color.r = 1  # cr
            marker1.color.g = 1  # cg
            marker1.color.b = 1  # cb

            marker1.color.a = 1
            marker1.scale.z = 1

            marker1.pose.orientation.w = 1.0
            marker1.pose.position.x = (ob[1]+ob[3])/2
            marker1.pose.position.y = (ob[9]+ob[11])/2
            marker1.pose.position.z = (ob[21]+ob[23])/2+1

            marker1.text = ob[0]+':'+str(np.floor(ob[25]*100)/100)
            csv_marker_array.append(marker1.text)

            marker_array_text.markers.append(marker1)
        if mnum > len(boxes):
            for obid in range(len(boxes), mnum):
                marker = Marker()
                marker.header.frame_id = 'livox_frame'
                marker.header.stamp = rospy.Time.now()
                marker.id = obid*2
                marker.action = Marker.ADD
                marker.type = Marker.LINE_LIST
                marker.lifetime = rospy.Duration(0.01)
                marker.color.r = 1
                marker.color.g = 1
                marker.color.b = 1
                marker.color.a = 0
                marker.scale.x = 0.2
                marker.points = []
                marker_array.markers.append(marker)

                marker1 = Marker()
                marker1.header.frame_id = 'livox_frame'
                marker1.header.stamp = rospy.Time.now()
                marker1.ns = "basic_shapes"

                marker1.id = obid*2+1
                marker1.action = Marker.ADD

                marker1.type = Marker.TEXT_VIEW_FACING

                marker1.lifetime = rospy.Duration(0.01)
                marker1.color.a = 0
                marker1.text = 'aaa'
                marker_array_text.markers.append(marker1)
        mnum = len(boxes)
        self.marker_pub.publish(marker_array)
        self.pointcloud_pub.publish(pointcloud_msg)
        self.marker_text_pub.publish(marker_array_text)
        t1 = time.time()
        #print('det_time(ms)', 1000*(t1-t0))
        self.csv_writer.writerow([msg.header.seq,
                                  1000*(t1-t0),
                                  self.ped_count,
                                  mnum, (str(csv_marker_array)).strip('"')])
        self.ped_count = 0
if __name__ == '__main__':
    livox = Detector()
    livox.register_exit_function()
    livox.main_func()
    #rospy.spin()
