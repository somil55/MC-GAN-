from keras.layers import Input, Dense, Reshape, Flatten, Dropout, multiply, Concatenate
from keras.layers import BatchNormalization, Activation, Embedding
from keras.layers import ZeroPadding2D, DepthwiseConv2D
from keras.layers.advanced_activations import LeakyReLU
from keras.layers.convolutional import UpSampling2D, Conv2D , Conv2DTranspose
from keras.models import Sequential, Model
from keras.optimizers import Adam
from scipy.misc import toimage, imsave
import keras
import datetime
import keras.backend as K
from keras.callbacks import TensorBoard
import matplotlib.pyplot as plt
import tensorflow as tf
import numpy as np
import pdb
import os
import cv2
import random

def get_condition_image(img):
	# image is 64 * 64 * 26
	batch_size = np.random.randint(4, 8, 1)[0] # number of letters to keep
	idx = random.sample(range(0, 26), batch_size)  # the indices to keep
	batch_cond_img = np.ones((batch_size+1, 64, 64, 26)) # input of glyph network

	for i in range(batch_size):
		batch_cond_img[i, :, :, :][:, :, idx] = img[:, :, idx]
		batch_cond_img[i, :, :, :][:, :, idx[i]] = np.ones((64, 64))
	batch_cond_img[-1, :, :, :][:, :, idx] = img[:, :, idx]

	return batch_cond_img, idx	

def write_log(callback, names, logs, batch_no):
	for name, value in zip(names, logs):
		summary = tf.Summary()
		summary_value = summary.value.add()
		summary_value.simple_value = value
		summary_value.tag = name
		callback.writer.add_summary(summary, batch_no)
		callback.writer.flush()

class ORNA_MODEL():
	def __init__(self):
		# Input shape
		self.img_rows = 64
		self.img_cols = 64
		self.channels = 3
		self.img_shape = (self.img_rows, self.img_cols, self.channels)

		# # Calculate output shape of D (PatchGAN)
		# Number of filters in the first layer of G and D
		self.gf = 64
		self.df = 52

		optimizer = Adam(0.0002, 0.5)
		# Build and compile the discriminator
		self.discriminator = self.build_discriminator()
		self.discriminator.compile(loss=['mse','mse'],
			optimizer=optimizer)


		# define output shape of discriminator
		patch_local = 4
		patch_global = 1
		self.disc_patch_local = (patch_local, patch_local, 1)
		self.disc_patch_global = (patch_global, patch_global, 1)


		#-------------------------
		# Construct Computational
		#   Graph of Generator
		#-------------------------

		# Build the generator
		self.generator = self.build_generator()

		# Input images and their conditioning images
		gt_img = Input(shape=self.img_shape)
		cond_img = Input(shape=self.img_shape)

		# By conditioning on B generate a fake version of A
		fake_img = self.generator(cond_img)
		sigmoid_layer = Activation('sigmoid')
		masked_fake_img = sigmoid_layer(fake_img)

		# For the combined model we will only train the generator
		self.discriminator.trainable = False

		# Discriminators determines validity of translated images / condition pairs
		valid_local , valid_global = self.discriminator([fake_img, cond_img])

		self.combined = Model(inputs=[gt_img, cond_img], outputs=[valid_local ,\
												 valid_global, fake_img, masked_fake_img ])
		self.combined.compile(loss=['mse', 'mse', 'mae', 'mse'],
							  loss_weights=[1, 10, 300, 300],
							  optimizer=optimizer)

	def build_generator(self):
		"""U-Net Generator"""

		def conv2d(layer_input, filters, f_size=4, bn=True):
			"""Layers used during downsampling"""
			d = Conv2D(filters, kernel_size=f_size, strides=2, padding='same')(layer_input)
			if bn:
				d = BatchNormalization(momentum=0.8)(d)
			d = LeakyReLU(alpha=0.2)(d)
			return d

		def deconv2d(layer_input, skip_input, filters, f_size=4, dropout_rate=0.4):
			"""Layers used during upsampling"""
			u = UpSampling2D(size=2)(layer_input)
			u = Conv2D(filters, kernel_size=f_size, strides=1, padding='same',\
												 activation='relu')(u)
			if dropout_rate:
				u = Dropout(dropout_rate)(u)
			u = BatchNormalization(momentum=0.8)(u)
			u = Concatenate()([u, skip_input])
			return u

		# Image input
		d0 = Input(shape=self.img_shape)

		# Downsampling
		d1 = conv2d(d0, self.gf, bn=False)
		d2 = conv2d(d1, self.gf*2)
		d3 = conv2d(d2, self.gf*4)
		d4 = conv2d(d3, self.gf*8)


		# Upsampling
		u1 = deconv2d(d4, d3, self.gf*8)
		u2 = deconv2d(u1, d2, self.gf*4)
		u3 = deconv2d(u2, d1, self.gf*2)
		u4 = UpSampling2D(size=2)(u3)
		output_img = Conv2D(self.channels, kernel_size=4, strides=1,\
						 padding='same', activation='tanh')(u4)

		# Model(d0, output_img).summary()
		return Model(d0, output_img)

	def build_discriminator(self):

		def d_layer(layer_input, filters, f_size=4, bn=True):
			"""Discriminator layer"""
			d = Conv2D(filters, kernel_size=f_size, strides=2, padding='same')(layer_input)
			d = LeakyReLU(alpha=0.2)(d)
			if bn:
				d = BatchNormalization(momentum=0.8)(d)
			return d

		gt_img = Input(shape=self.img_shape)
		cond_img = Input(shape=self.img_shape)

		# Concatenate image and conditioning image by channels to produce input
		combined_imgs = Concatenate(axis=-1)([gt_img, cond_img])

		d1 = d_layer(combined_imgs, self.df, bn=False)
		d2 = d_layer(d1, self.df*2)
		d3 = d_layer(d2, self.df*4)
		d4 = d_layer(d3, self.df*8)
		d5 = d_layer(d4, self.df*16)
		d6 = d_layer(d5, self.df*16)

		validity_local = Conv2D(1, kernel_size=4, strides=1, padding='same')(d4)
		validity_global = Conv2D(1, kernel_size=4, strides=1, padding='same')(d6)

		# Model([gt_img, cond_img], [validity_local , validity_global]).summary()
		return Model([gt_img, cond_img], [validity_local,validity_global])

	def train(self, epochs, batch_size=26, sample_interval=500):

		start_time = datetime.datetime.now()

		# add divyansh's dataloader functions
		
		data_dir = 'datasets/Capitals_colorGrad64/train/'
		files = os.listdir(data_dir)
		gt_imgs = []
		gt_imgs_color = []				

		self.block_size = 50
		for count, file in enumerate(files):
			img = cv2.imread(data_dir + file , 0)
			img_color = cv2.imread(data_dir + file)
			new_img = []
			new_img_color = []
			for i in range(26):
				new_img.append(img[:,64*i:64*(i+1)])
				new_img_color.append(img_color[:, 64*i:64*(i+1)])
			new_img = np.array(new_img)
			new_img_color = np.array(new_img_color)
			new_img = np.transpose(new_img , (1,2,0))
			gt_imgs.append(new_img)
			gt_imgs_color.append(new_img_color)
			print(file)
			if count>=self.block_size:
				break
		gt_imgs = np.array(gt_imgs)
		gt_imgs = ( gt_imgs.astype( np.float32 ) - 127.5 ) / 127.5

		gt_imgs_color = np.array(gt_imgs_color)
		gt_imgs_color = (gt_imgs_color.astype(np.float32) - 127.5) / 127.5


		# Adversarial loss ground truths
		valid_local = np.ones((batch_size,) + self.disc_patch_local)
		valid_global = np.ones((batch_size,) + self.disc_patch_global)
		fake_local = np.zeros((batch_size,) + self.disc_patch_local)
		fake_global = np.zeros((batch_size,) + self.disc_patch_global)


		log_path = 'graphs/orna/'
		callback = TensorBoard(log_path)
		callback.set_model(self.combined)
		train_names = ['dloss_local','dloss_global',\
		'gloss_local','gloss_global','gloss_L1']

		model_path = 'saved_models/glyph_net'+str(self.block_size)+'/6500/generator.json'
		weights_path = 'saved_models/glyph_net'+str(self.block_size)+'/6500/generator_weights.hdf5'

		json_file = open(model_path, 'r')
		loaded_model_json = json_file.read()
		json_file.close()
		glyph_generator = keras.models.model_from_json(loaded_model_json)
		glyph_generator.load_weights(weights_path)

		for epoch in range(epochs):
			for font_num in range(gt_imgs.shape[0]):
				#idx = np.random.randint(0, gt_imgs.shape[0], 1)
				idx = [font_num]
				cond_glyph_imgs_batch, img_indices = get_condition_image(gt_imgs[idx, :, :, :][0])
				glyphs_output = glyph_generator.predict(cond_glyph_imgs_batch)
				num_inp_fonts = len(img_indices)
				orna_gen_input = glyphs_output[num_inp_fonts, :, :, :]
				for i in range(num_inp_fonts):
					orna_gen_input[:,:,img_indices[i]] = glyphs_output[i, :, :, img_indices[i]]
				cond_imgs_batch = np.array([orna_gen_input, orna_gen_input, orna_gen_input])
				cond_imgs_batch = np.transpose(cond_imgs_batch, (3,1,2,0))
				
				gt_imgs_batch = gt_imgs_color[idx][0]

				# ---------------------
				#  Train Discriminator
				# ---------------------

				# Condition on B and generate a translated version
				fake_img_batch = self.generator.predict(cond_imgs_batch)
				# Train the discriminators (original images = real / generated = Fake)
				_ ,d_loss_real_local, d_loss_real_global = \
					self.discriminator.train_on_batch([gt_imgs_batch, cond_imgs_batch],\
						[valid_local,valid_global])
				_ ,d_loss_fake_local, d_loss_fake_global = \
					self.discriminator.train_on_batch([fake_img_batch, cond_imgs_batch],\
						[fake_local,fake_global])
			   
				d_loss_global = 0.5 * np.add(d_loss_real_global, d_loss_fake_global)
				d_loss_local = 0.5 * np.add(d_loss_real_local, d_loss_fake_local)

				# -----------------
				#  Train Generator
				# -----------------

				# Train the generators
				g_loss = self.combined.train_on_batch([gt_imgs_batch, cond_imgs_batch],\
				 [valid_local , valid_global, gt_imgs_batch, 1/(1+np.exp(-cond_imgs_batch))])

				elapsed_time = datetime.datetime.now() - start_time

				write_log(callback, train_names, \
					np.asarray([d_loss_local, d_loss_global, g_loss[0], \
					 g_loss[1], g_loss[2]]), epoch)
				# Plot the progress
				print ("[Epoch %d] [D loss0: %f, D loss1: %f]\
				 [G loss0: %f , G loss1: %f , G loss2: %f] time: %s" % (epoch, d_loss_local,\
				  d_loss_global, g_loss[1], g_loss[2], g_loss[3], str(elapsed_time)))

				if epoch % 500 == 0:
					output_dir = 'results/orna'+str(epoch)
					os.makedirs(output_dir, exist_ok=True)
					for i in range(0, 26):
						res = toimage(np.uint8(fake_img_batch[i, :, :, :, ]*127.5+127.5))
						imsave(output_dir+'/generated_'+str(font_num)+'_'+str(i)+'.png', res)
						res = toimage(np.uint8(gt_imgs_batch[i, :, :, :, ]*127.5+127.5))
						imsave(output_dir+'/groundTruth'+str(font_num)+'_'+str(i)+'.png', res)
						res = toimage(np.uint8(orna_gen_input[:, :, i]*127.5+127.5))
						imsave(output_dir+'/glyphnet'+str(font_num)+'_'+str(i)+'.png', res)
				if epoch % sample_interval == 0:
					self.save_model(epoch)



	def sample_images(self, epoch, cond_imgs, gt_imgs):
		data_dir = '../results/ORNA_net/'
		os.makedirs(data_dir, exist_ok=True)

		fake_imgs = self.generator.predict(cond_imgs)
		for i in range(cond_imgs.shape[0]):
			row, col = 3, cond_imgs.shape[3]

			cond_img = cond_imgs[i]
			gt_img = gt_imgs[i]
			fake_img = fake_imgs[i]

			# Rescale images 0 - 1
			fake_img = 0.5*fake_img + 0.5
			cond_img = 0.5*cond_img + 0.5
			gt_img = 0.5*gt_img + 0.5

			titles = ['Condition', 'Generated', 'Original']
			fig, axs = plt.subplots(row, col)
			for r in range(row):
				for c in range(col):
					if r==0:
						axs[r,c].imshow(cond_img[:,:,c])

					if r==1:
						axs[r,c].imshow(fake_img[:,:,c])

					if r==2:
						axs[r,c].imshow(gt_img[:,:,c])
						
					axs[r,c].axis('off')
			fig.savefig(data_dir + "%d_%d.png" % (epoch , i))
			plt.close()

	def save_model(self , epoch):

		def save(model, model_name):
			data_dir = "saved_models/orna_net"+str(self.block_size)+"/"+str(epoch)
			os.makedirs(data_dir, exist_ok=True)
			model_path = data_dir + "/%s.json" % (model_name)
			weights_path = data_dir + "/%s_weights.hdf5" % (model_name)
			options = {"file_arch": model_path,
						"file_weight": weights_path}
			json_string = model.to_json()
			open(options['file_arch'], 'w').write(json_string)
			model.save_weights(options['file_weight'])

		save(self.generator, "generator_orna")
		save(self.discriminator, "discriminator_orna")  


ORNA_model = ORNA_MODEL()
ORNA_model.train(epochs=10000)
