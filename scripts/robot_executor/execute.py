from __future__ import print_function
import os
import sys
import time
import rospy
import rospkg
import logging
import numpy as np
from config import ik_config
from geometry_msgs.msg import Twist
from os.path import join, expanduser
from torobo_forward import forward_kinematics
from trac_ik_python.trac_ik_wrap import TRAC_IK
from trac_ik_python.trac_ik import IK
from torobo_ik.srv import SolveDiffIK, SolveDiffIKRequest, SolveDiffIKResponse


rospack = rospkg.RosPack()
lyap = rospack.get_path('lyapunovlearner')

sys.path.append(join(lyap, 'ToroboTakahashi'))
sys.path.append(join(lyap, 'ToroboTakahashi', 'tampy'))

from tampy.tampy import Tampy
from tampy.tampy import ORDER_SERVO_ON, ORDER_SERVO_OFF, ORDER_RUN_MODE, CTRL_MODE_CURRENT

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)
logger = logging.getLogger(__name__)

class ToroboExecutor(object):
	def __init__(self, home_pos, urdf):
		"""
		 in update(self, currents) currents are inindividual currents to each joint
		 According to Ryo-san, the max current is 0.5A and you should start with 0.05A or sth and gradually increase
		"""
		self.tampy = Tampy()
		self.tampy.servo_on()
		self.urdf  = urdf
		self.home_pos = home_pos
		self.control_freq = 30.0
		self.latest_control = time.time()
		self.carts_to_send = rospy.ServiceProxy("/torobo_ik/solve_diff_ik", SolveDiffIK)

	def send_carts(self, msg):
		rospy.wait_for_service("/torobo_ik/solve_diff_ik")

		try:
			resp = self.carts_to_send(msg)
			return SolveDiffIKResponse(resp.q_out)
		except rospy.ServiceException, e:
			print( "Service call failed: %s"%(e))

	def set_position(self, positions, duration=500):
		each_time = 5.00
		motion_time = 10.00

		self.tampy.start_joints()
		self.tampy.log_buf(duration)
		self.tampy.start_timer()

		self.tampy.move_joints(positions, each_time=each_time, motion_time=motion_time, duration=duration)

	def update(self, currents):
		self.tampy.send_currents(currents)
		time.sleep(max(
			self.latest_control + 1.0 / self.control_freq - time.time(),
			0
		))
		self.latest_control = time.time()
		return self.get_state()

	def get_state(self):
		rx = self.tampy.get_latest_rx()
		positions = [j.position for j in rx.joints]
		velocities = [j.velocity for j in rx.joints]
		T_mat = np.array([forward_kinematics(positions)])
		endeffector = (T_mat[:, 0:3, 3] / 1000.0).reshape(3)

		return positions, velocities, endeffector

	@staticmethod
	def expand(x, axis):
		return np.expand_dims(x, axis)

	def cart_to_ik_request_msg(self, cart_pos, q_in=None):
		msg = Twist()
		msg.linear.x  = -0.191556
		msg.linear.y  = -0.216731
		msg.linear.z  =  0.963396
		msg.angular.x = -2.05147
		msg.angular.y = -2.32108
		msg.angular.z = -0.766848

		if q_in is None:
			q_in = [0,0,0,0,0,0,0]

		tosend 				= SolveDiffIKRequest()
		tosend.desired_vel  = msg
		tosend.q_in 		= q_in

		return self.send_carts(tosend)

	def optimize_traj(self, data, stab_handle, opt_exec):
		"""
			function runs motions learned with SEDs
			xd = f(x)

			where x is an arbitrary d dimensional variable and xd is the first time derivative
		"""
		x_des       = data[:, -1]   # as visualized from converted dataset with ik_sub
		d = data.shape[0]/2

		first_time = True
		exit_while_loop = False

		_, _, x_cur = self.get_state()
		diff = np.linalg.norm((x_cur[:3] - x_des[:3]), ord=2)
		i = 0
		while not (rospy.is_shutdown()) and (diff > opt_exec['stop_tol']):
			t1 = time.time()
			# these are in joint coordinates save x_cur
			q_cur, qdot_cur, x_cur = self.get_state()
			t2 = time.time()

			t_diff = opt_exec['stop_tol']
			xdot_cur = x_cur / t_diff

			xvel_des = sum(stab_handle(data - np.expand_dims(x_des, 1))) #f + u

			# take it to joint space
			# for i in range(xvel_des.shape[-1]):
			x_next = self.expand(x_cur[:d], 1) + xvel_des *  opt_exec['dt'] #  opt_exec['dt']#
			print('x_next[:, i]: ', x_next[:, i], ' t2-t1: ', t_diff)

			q_cur, _, _     = self.get_state()
			joint_positions = list(self.cart_to_ik_request_msg(x_next, q_in=q_cur).q_out)
			target_position = list(self.cart_to_ik_request_msg(x_des).q_out)
			print('joint_positions: ', joint_positions)

			self.set_position(joint_positions)

			diff = np.linalg.norm((x_next[:, i] - x_des[:3]), ord=2)
			rospy.loginfo('diff: {}'.format(diff))
			i += 1

			first_time = False

		return x_next, xvel_des

	def __enter__(self):
		self.set_position(self.home_pos)
		self.tampy.send(ORDER_RUN_MODE, value1=CTRL_MODE_CURRENT)
		self.tampy.send(ORDER_SERVO_ON)
		self.latest_control = time.time()
		return self

	def __exit__(self, type, value, tb):
		self.tampy.send_currents([0] * 7)
		# TODO: why do we need multiple calls to kill?
		for _ in range(3):
			self.tampy.send(ORDER_SERVO_OFF)
