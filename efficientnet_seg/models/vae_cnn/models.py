# Keras implementation of the paper:
# 3D MRI Brain Tumor Segmentation Using Autoencoder Regularization
# by Myronenko A. (https://arxiv.org/pdf/1810.11654.pdf)
# Author of this code: Suyog Jadhav (https://github.com/IAmSUyogJadhav)

import tensorflow.keras.backend as K
from tensorflow.keras.losses import mse
from tensorflow.keras.layers import Conv2D, Activation, Add, UpSampling2D, Lambda, Dense, \
                                    Input, Reshape, Flatten, Dropout
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.models import Model

from efficientnet_seg.models.vae_cnn.group_norm import GroupNormalization
from efficientnet_seg.models.losses_metrics import my_iou_metric

def green_block(inp, filters, data_format="channels_last", name=None):
    """
    green_block(inp, filters, name=None)
    ------------------------------------
    Implementation of the special residual block used in the paper. The block
    consists of two (GroupNorm --> ReLu --> 3x3x3 non-strided Convolution)
    units, with a residual connection from the input `inp` to the output. Used
    internally in the model. Can be used independently as well.
    Parameters
    ----------
    `inp`: An keras.layers.layer instance, required
        The keras layer just preceding the green block.
    `filters`: integer, required
        No. of filters to use in the 3D convolutional block. The output  layer of this green block will have this many no. of channels.
    `data_format`: string, optional
        The format of the input data. Must be either 'chanels_first' or  'channels_last'. Defaults to `channels_first`, as used in the paper.
    `name`: string, optional
        The name to be given to this green block. Defaults to None, in which  case, keras uses generated names for the involved layers. If a string  is provided, the names of individual layers are generated by attaching  a relevant prefix from [GroupNorm_, Res_, Conv2D_, Relu_, ], followed  by _1 or _2.
    Returns
    -------
    `out`: A keras.layers.Layer instance
        The output of the green block. Has no. of channels equal to `filters`.  The size of the rest of the dimensions remains same as in `inp`.
    """
    inp_res = Conv2D(filters=filters, kernel_size=(1, 1), strides=1, data_format=data_format, name=f'Res_{name}' if name else None)(inp)

    # axis=1 for channels_first data format
    # No. of groups = 8, as given in the paper
    x = GroupNormalization(groups=8, axis=1 if data_format == 'channels_first' else -1, name=f'GroupNorm_1_{name}' if name else None)(inp)
    x = Activation('relu', name=f'Relu_1_{name}' if name else None)(x)
    x = Conv2D(filters=filters, kernel_size=(3, 3), strides=1, padding='same', data_format=data_format, name=f'Conv2D_1_{name}' if name else None)(x)

    x = GroupNormalization(groups=8, axis=1 if data_format == 'channels_first' else -1, name=f'GroupNorm_2_{name}' if name else None)(x)
    x = Activation('relu', name=f'Relu_2_{name}' if name else None)(x)
    x = Conv2D(filters=filters, kernel_size=(3, 3), strides=1, padding='same', data_format=data_format, name=f'Conv2D_2_{name}' if name else None)(x)

    out = Add(name=f'Out_{name}' if name else None)([x, inp_res])
    return out


# From keras-team/keras/blob/master/examples/variational_autoencoder.py
def sampling(args):
    """Reparameterization trick by sampling from an isotropic unit Gaussian.
    # Arguments
        args (tensor): mean and log of variance of Q(z|X)
    # Returns
        z (tensor): sampled latent vector
    """
    z_mean, z_var = args
    batch = K.shape(z_mean)[0]
    dim = K.int_shape(z_mean)[1]
    # by default, random_normal has mean = 0 and std = 1.0
    epsilon = K.random_normal(shape=(batch, dim))
    return z_mean + K.exp(0.5 * z_var) * epsilon


def dice_coefficient(y_true, y_pred):
    y_true_f = K.flatten(y_true)
    y_pred_f = K.flatten(y_pred)
    intersection = K.sum(K.abs(y_true_f * y_pred_f), axis=-1)
    return (2. * intersection) / ( K.sum(K.square(y_true_f), -1) + K.sum(K.square(y_pred_f), -1) + 1e-8)


def loss(input_shape, inp, out_VAE, z_mean, z_var, e=1e-8, weight_L2=0.1, weight_KL=0.1, data_format="channels_last"):
    """
    loss(input_shape, inp, out_VAE, z_mean, z_var, e=1e-8, weight_L2=0.1, weight_KL=0.1)
    ------------------------------------------------------
    Since keras does not allow custom loss functions to have arguments
    other than the true and predicted labels, this function acts as a wrapper
    that allows us to implement the custom loss used in the paper, involving
    outputs from multiple layers.
    L = - L<dice> + weight_L2 ∗ L<L2> + weight_KL ∗ L<KL>
    - L<dice> is the dice loss between input and segmentation output.
    - L<L2> is the L2 loss between the output of VAE part and the input.
    - L<KL> is the standard KL divergence loss term for the VAE.
    Parameters
    ----------
    `input_shape`: A 4-tuple, required
        The shape of an image as the tuple (c, H, W, D), where c is  the no. of channels; H, W and D is the height, width and depth of the  input image, respectively.
    `inp`: An keras.layers.Layer instance, required
        The input layer of the model. Used internally.
    `out_VAE`: An keras.layers.Layer instance, required
        The output of VAE part of the decoder. Used internally.
    `z_mean`: An keras.layers.Layer instance, required
        The vector representing values of mean for the learned distribution
        in the VAE part. Used internally.
    `z_var`: An keras.layers.Layer instance, required
        The vector representing values of variance for the learned distribution
        in the VAE part. Used internally.
    `e`: Float, optional
        A small epsilon term to add in the denominator to avoid dividing by
        zero and possible gradient explosion.
    `weight_L2`: A real number, optional
        The weight to be given to the L2 loss term in the loss function. Adjust to get best
        results for your task. Defaults to 0.1.
    `weight_KL`: A real number, optional
        The weight to be given to the KL loss term in the loss function. Adjust to get best
        results for your task. Defaults to 0.1.
    Returns
    -------
    loss_(y_true, y_pred): A custom keras loss function
        This function takes as input the predicted and ground labels, uses them
        to calculate the dice loss. Combined with the L<KL> and L<L2 computed
        earlier, it returns the total loss.
    """
    if data_format == "channels_last":
        H, W, c = input_shape
        n = c * H * W
    elif data_format == "channels_first":
        c, H, W = input_shape
        n = c * H * W

    #loss_L2 = mse(inp, out_VAE)
    loss_L2 = K.mean(K.square(inp - out_VAE), axis=(1, 2, 3))

    loss_KL = (1 / n) * K.sum(K.exp(z_var) + K.square(z_mean) - 1. - z_var, axis=-1)

    def loss_(y_true, y_pred):
        y_true_f = K.flatten(y_true)
        y_pred_f = K.flatten(y_pred)
        intersection = K.sum(K.abs(y_true_f * y_pred_f), axis=-1)
        loss_dice = (2. * intersection) / (K.sum(K.square(y_true_f), -1) + K.sum(K.square(y_pred_f), -1) + e)
        return - loss_dice + weight_L2 * loss_L2 + weight_KL * loss_KL
    return loss_


def build_model(input_shape=(160, 192, 4), output_channels=3, weight_L2=0.1, weight_KL=0.1, data_format="channels_last"):
    """
    build_model(input_shape=(160, 192, 4), output_channels=3, weight_L2=0.1, weight_KL=0.1)
    -------------------------------------------
    Creates the model used in the BRATS2018 winning solution
    by Myronenko A. (https://arxiv.org/pdf/1810.11654.pdf)
    Parameters
    ----------
    `input_shape`: A 4-tuple, optional.
        Shape of the input image. Must be a 4D image of shape (c, H, W, D),  where, each of H, W and D are divisible by 2^4, and c is divisible by 4.
        Defaults to the crop size used in the paper, i.e., (4, 160, 192, 128).
    `output_channels`: An integer, optional.
        The no. of channels in the output. Defaults to 3 (BraTS 2018 format).
    `weight_L2`: A real number, optional
        The weight to be given to the L2 loss term in the loss function. Adjust to get best
        results for your task. Defaults to 0.1.
    `weight_KL`: A real number, optional
        The weight to be given to the KL loss term in the loss function. Adjust to get best
        results for your task. Defaults to 0.1.\
    `data_format`: str. optional.
        either `channels_last` or `channels_first`
    Returns
    -------
    `model`: A keras.models.Model instance
        The created model.
    """
    if data_format == "channels_last":
        H, W, c = input_shape
    elif data_format == "channels_first":
        c, H, W = input_shape
    assert len(input_shape) == 3, "Input shape must be a 3-tuple"
    # assert (c % 4) == 0, "The no. of channels must be divisible by 4"
    assert (H % 16) == 0 and (W % 16) == 0, \
        "All the input dimensions must be divisible by 16"


    # -------------------------------------------------------------------------
    # Encoder
    # -------------------------------------------------------------------------

    ## Input Layer
    inp = Input(input_shape)

    ## The Initial Block
    x = Conv2D(filters=32, kernel_size=(3, 3), strides=1, padding='same', data_format=data_format, name='Input_x1')(inp)

    ## Dropout (0.2)
    x = Dropout(0.2)(x)

    ## Green Block x1 (output filters = 32)
    x1 = green_block(x, 32, name='x1')
    x = Conv2D(filters=32, kernel_size=(3, 3), strides=2, padding='same', data_format=data_format, name='Enc_DownSample_32')(x1)

    ## Green Block x2 (output filters = 64)
    x = green_block(x, 64, name='Enc_64_1')
    x2 = green_block(x, 64, name='x2')
    x = Conv2D(filters=64, kernel_size=(3, 3), strides=2, padding='same', data_format=data_format, name='Enc_DownSample_64')(x2)

    ## Green Blocks x2 (output filters = 128)
    x = green_block(x, 128, name='Enc_128_1')
    x3 = green_block(x, 128, name='x3')
    x = Conv2D(filters=128, kernel_size=(3, 3), strides=2, padding='same', data_format=data_format, name='Enc_DownSample_128')(x3)

    ## Green Blocks x4 (output filters = 256)
    x = green_block(x, 256, name='Enc_256_1')
    x = green_block(x, 256, name='Enc_256_2')
    x = green_block(x, 256, name='Enc_256_3')
    x4 = green_block(x, 256, name='x4')

    # -------------------------------------------------------------------------
    # Decoder
    # -------------------------------------------------------------------------

    ## GT (Groud Truth) Part
    # -------------------------------------------------------------------------

    ### Green Block x1 (output filters=128)
    x = Conv2D(filters=128, kernel_size=(1, 1), strides=1, data_format=data_format, name='Dec_GT_ReduceDepth_128')(x4)
    x = UpSampling2D(size=2, data_format=data_format, name='Dec_GT_UpSample_128')(x)
    x = Add(name='Input_Dec_GT_128')([x, x3])
    x = green_block(x, 128, name='Dec_GT_128')

    ### Green Block x1 (output filters=64)
    x = Conv2D(filters=64, kernel_size=(1, 1), strides=1, data_format=data_format, name='Dec_GT_ReduceDepth_64')(x)
    x = UpSampling2D(size=2, data_format=data_format, name='Dec_GT_UpSample_64')(x)
    x = Add(name='Input_Dec_GT_64')([x, x2])
    x = green_block(x, 64, name='Dec_GT_64')

    ### Green Block x1 (output filters=32)
    x = Conv2D(filters=32, kernel_size=(1, 1), strides=1, data_format=data_format, name='Dec_GT_ReduceDepth_32')(x)
    x = UpSampling2D(size=2, data_format=data_format, name='Dec_GT_UpSample_32')(x)
    x = Add(name='Input_Dec_GT_32')([x, x1])
    x = green_block(x, 32, name='Dec_GT_32')

    ### Blue Block x1 (output filters=32)
    x = Conv2D(filters=32, kernel_size=(3, 3), strides=1, padding='same', data_format=data_format, name='Input_Dec_GT_Output')(x)

    ### Output Block
    out_GT = Conv2D(filters=output_channels, kernel_size=(1, 1), strides=1, data_format=data_format, activation='sigmoid', name='Dec_GT_Output')(x)
    # No. of tumor classes is 3

    ## VAE (Variational Auto Encoder) Part
    # -------------------------------------------------------------------------

    ### VD Block (Reducing dimensionality of the data)
    x = GroupNormalization(groups=8, axis=1 if data_format == 'channels_first' else -1, name='Dec_VAE_VD_GN')(x4)
    x = Activation('relu', name='Dec_VAE_VD_relu')(x)
    x = Conv2D(filters=16, kernel_size=(3, 3), strides=2, padding='same', data_format=data_format, name='Dec_VAE_VD_Conv2D')(x)

    # Not mentioned in the paper, but the author used a Flattening layer here.
    x = Flatten(name='Dec_VAE_VD_Flatten')(x)
    x = Dense(256, name='Dec_VAE_VD_Dense')(x)

    ### VDraw Block (Sampling)
    z_mean = Dense(128, name='Dec_VAE_VDraw_Mean')(x)
    z_var = Dense(128, name='Dec_VAE_VDraw_Var')(x)
    x = Lambda(sampling, name='Dec_VAE_VDraw_Sampling')([z_mean, z_var])

    ### VU Block (Upsizing back to a depth of 256)
    if c < 4:
        x = Dense(c * (H//16) * (W//16))(x)
        x = Activation('relu')(x)
        x = Reshape(((H//16), (W//16), c))(x)
    else:
        assert (c % 4) == 0, "The no. of channels must be divisible by 4"
        x = Dense((c//4) * (H//16) * (W//16))(x)
        x = Activation('relu')(x)
        x = Reshape(((H//16), (W//16), (c//4)))(x)
    x = Conv2D(filters=256,kernel_size=(1, 1), strides=1, data_format=data_format, name='Dec_VAE_ReduceDepth_256')(x)
    x = UpSampling2D(size=2, data_format=data_format,name='Dec_VAE_UpSample_256')(x)

    ### Green Block x1 (output filters=128)
    x = Conv2D(filters=128, kernel_size=(1, 1), strides=1, data_format=data_format, name='Dec_VAE_ReduceDepth_128')(x)
    x = UpSampling2D(size=2, data_format=data_format, name='Dec_VAE_UpSample_128')(x)
    x = green_block(x, 128, name='Dec_VAE_128')

    ### Green Block x1 (output filters=64)
    x = Conv2D(filters=64, kernel_size=(1, 1), strides=1, data_format=data_format, name='Dec_VAE_ReduceDepth_64')(x)
    x = UpSampling2D(size=2, data_format=data_format, name='Dec_VAE_UpSample_64')(x)
    x = green_block(x, 64, name='Dec_VAE_64')

    ### Green Block x1 (output filters=32)
    x = Conv2D(filters=32, kernel_size=(1, 1), strides=1, data_format=data_format, name='Dec_VAE_ReduceDepth_32')(x)
    x = UpSampling2D(size=2, data_format=data_format, name='Dec_VAE_UpSample_32')(x)
    x = green_block(x, 32, name='Dec_VAE_32')

    ### Blue Block x1 (output filters=32)
    x = Conv2D(filters=32, kernel_size=(3, 3), strides=1, padding='same', data_format=data_format, name='Input_Dec_VAE_Output')(x)

    ### Output Block
    out_VAE = Conv2D(filters=4, kernel_size=(1, 1), strides=1, data_format=data_format, name='Dec_VAE_Output')(x)

    # Build and Compile the model
    out = out_GT
    model = Model(inp, out)  # Create the model
    model.compile(Adam(lr=1e-4),
                  loss(input_shape, inp, out_VAE, z_mean, z_var, weight_L2=weight_L2,
                       weight_KL=weight_KL, data_format=data_format),
                  metrics=[my_iou_metric]
                )

    return model

if __name__ == "__main__":
    model = build_model(output_channels=1)
    model.summary()
