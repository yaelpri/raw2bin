import os
import suite2p 
import shutil
import numpy as np 

from pathlib import Path
from conftest import initialize_ops #Guarantees that tests and this script use the same ops
from tests.regression.utils import FullPipelineTestUtils, DetectionTestUtils
from suite2p.extraction import masks
test_data_dir = 'test_data'
# Assumes the input file has already been downloaded
test_input_dir_path = '/home/stringlab/Desktop/suite2p/data/test_data/'
# Output directory where suite2p results are kept
test_output_dir_path = '/home/stringlab/Desktop/suite2p/scripts/test_data'

class GenerateFullPipelineTestData:
	# Full Pipeline Tests
	def generate_1p1c1500_expected_data(ops):
		"""
		Generates expected output for test_1plane_1chan_with_batches_metrics_and_exported_to_nwb_format
		for test_full_pipeline.py
		"""
		test_ops = FullPipelineTestUtils.initialize_ops_test1plane_1chan_with_batches(ops.copy())
		suite2p.run_s2p(ops=test_ops)
		rename_output_dir('1plane1chan1500')

	# def generate_1p2c_expected_data(ops):
	#   """
	#   Generates expected output for test_1plane_2chan_sourcery of test_full_pipeline.py.
	#   """
	#   test_ops = FullPipelineTestUtils.initialize_ops_test_1plane_2chan_sourcery(ops.copy())
	#   suite2p.run_s2p(ops=test_ops)
	#   rename_output_dir('1plane2chan')

	def generate_2p2c1500_expected_data(ops):
		"""
		Generates expected output for test_2plane_2chan_with_batches of test_full_pipeline.py.
		"""
		test_ops = FullPipelineTestUtils.initialize_ops_test2plane_2chan_with_batches(ops.copy())
		suite2p.run_s2p(ops=test_ops)
		rename_output_dir('2plane2chan1500')

	def generate_2p2zmesoscan_expected_data(ops):
		"""
		Generates expected output for test_mesoscan_2plane_2z of test_full_pipeline.py.
		"""
		test_ops = FullPipelineTestUtils.initialize_ops_test_mesoscan_2plane_2z(ops.copy())
		suite2p.run_s2p(ops=test_ops)
		rename_output_dir('mesoscan')

	def generate_all_data(full_ops):
		# Expected Data for test_full_pipeline.py
		GenerateFullPipelineTestData.generate_1p1c1500_expected_data(full_ops)
		# generate_1p2c_expected_data(ops)
		GenerateFullPipelineTestData.generate_2p2c1500_expected_data(full_ops)
		GenerateFullPipelineTestData.generate_2p2zmesoscan_expected_data(full_ops)

class GenerateDetectionTestData:
	# Detection Tests
	def generate_detection_1plane1chan_test_data(ops):
		"""
		Generates expected output for test_detection_output_1plane1chan of test_detection_pipeline.py.
		"""
		# Use only the smaller input tif
		ops.update({
			'tiff_list': ['input.tif'],
		})
		ops = DetectionTestUtils.prepare(
			ops,
			[[Path(ops['data_path'][0]).joinpath('detection/pre_registered.npy')]],
			(404, 360)
		)
		ops, stat = suite2p.detection.detect(ops[0])
		cell_masks = masks.create_masks(stat, ops['Ly'], ops['Lx'], ops=ops)[0]
		output_dict = {
			'stat': stat,
			'cell_masks': cell_masks
		}
		np.save('expected_detect_output_1p1c0.npy', output_dict)
		# Remove suite2p directory generated by prepare function
		shutil.rmtree(os.path.join(test_output_dir_path, 'suite2p'))

	def generate_detection_2plane2chan_test_data(ops):
		ops.update({
			'nchannels': 2,
			'nplanes': 2,
		})
		detection_dir = test_ops['data_path'][0].joinpath('detection')
		ops = utils.DetectionTestUtils.prepare(
			test_ops,
			[
				[detection_dir.joinpath('pre_registered01.npy'), detection_dir.joinpath('pre_registered02.npy')],
				[detection_dir.joinpath('pre_registered11.npy'), detection_dir.joinpath('pre_registered12.npy')]
			]
			, (404, 360),
		)
		ops[0]['meanImg_chan2'] = np.load(detection_dir.joinpath('meanImg_chan2p0.npy'))
		ops[1]['meanImg_chan2'] = np.load(detection_dir.joinpath('meanImg_chan2p1.npy'))
		detect_wrapper(ops)
		nplanes = test_ops['nplanes']

	def generate_all_data(ops):
		 GenerateDetectionTestData.generate_detection_1plane1chan_test_data(ops)

def rename_output_dir(new_dir_name):
	curr_dir_path = os.path.abspath(os.getcwd())
	if os.path.exists(os.path.join(test_output_dir_path, new_dir_name)):
		shutil.rmtree(os.path.join(test_output_dir_path, new_dir_name))
	os.rename(os.path.join(test_output_dir_path, 'suite2p'), os.path.join(test_output_dir_path, new_dir_name))

def main():
	#Create test_data directory if necessary
	if not os.path.exists(test_data_dir):
		os.makedirs(test_data_dir)
		print('Created test directory at ' + os.path.abspath(test_data_dir))
	full_ops = initialize_ops(test_data_dir, test_input_dir_path)
	#GenerateFullPipelineTestData.generate_all_data(full_ops)
	det_ops = initialize_ops(test_data_dir, test_input_dir_path)
	GenerateDetectionTestData.generate_all_data(det_ops)
	return 

if __name__ == '__main__':
	main()
