import warnings
from functools import lru_cache
from typing import Tuple

import numpy as np
from numba import float32, njit, prange
from numpy import fft
from scipy.fftpack import next_fast_len

try:
    from mkl_fft import fft2, ifft2
except ModuleNotFoundError:
    warnings.warn("mkl_fft not installed.  Install it with conda: conda install mkl_fft", ImportWarning)

from .utils import addmultiply, spatial_taper, gaussian_fft, apply_dotnorm


def kernelD(xs: np.ndarray, ys: np.ndarray, sigL: float = 0.85) -> np.ndarray:
    """Gaussian kernel from xs (1D array) to ys (1D array), with the 'sigL' smoothing width for up-sampling kernels, (best between 0.5 and 1.0)"""
    xs0, xs1 = np.meshgrid(xs, xs)
    ys0, ys1 = np.meshgrid(ys, ys)
    dxs = xs0.reshape(-1, 1) - ys0.reshape(1, -1)
    dys = xs1.reshape(-1, 1) - ys1.reshape(1, -1)
    K = np.exp(-(dxs ** 2 + dys ** 2) / (2 * sigL ** 2))
    return K


def kernelD2(xs: int, ys: int) -> np.ndarray:
    ys, xs = np.meshgrid(xs, ys)
    ys = ys.flatten().reshape(1, -1)
    xs = xs.flatten().reshape(1, -1)
    R = np.exp(-((ys - ys.T) ** 2 + (xs - xs.T) ** 2))
    R = R / np.sum(R, axis=0)
    return R


@lru_cache(maxsize=5)
def mat_upsample(lpad, subpixel: int = 10):
    """ upsampling matrix using gaussian kernels """
    lar = np.arange(-lpad, lpad + 1)
    larUP = np.arange(-lpad, lpad + .001, 1. / subpixel)
    nup = larUP.shape[0]
    Kmat = np.linalg.inv(kernelD(lar, lar)) @ kernelD(lar, larUP)
    return Kmat, nup


def calculate_nblocks(L: int, block_size: int = 128) -> Tuple[int, int]:
    """Returns block_size and nblocks from dimension length and desired block size"""
    return (L, 1) if block_size >= L else (block_size, int(np.ceil(1.5 * L / block_size)))


def make_blocks(Ly, Lx, block_size=(128, 128)):
    """ computes overlapping blocks to split FOV into to register separately"""

    block_size_y, ny = calculate_nblocks(L=Ly, block_size=block_size[0])
    block_size_x, nx = calculate_nblocks(L=Lx, block_size=block_size[1])
    block_size = (block_size_y, block_size_x)

    # todo: could rounding to int here over-represent some pixels over others?
    ystart = np.linspace(0, Ly - block_size[0], ny).astype('int')
    xstart = np.linspace(0, Lx - block_size[1], nx).astype('int')
    yblock = [np.array([ystart[iy], ystart[iy] + block_size[0]]) for iy in range(ny) for _ in range(nx)]
    xblock = [np.array([xstart[ix], xstart[ix] + block_size[1]]) for _ in range(ny) for ix in range(nx)]

    NRsm = kernelD2(xs=np.arange(nx), ys=np.arange(ny)).T

    return yblock, xblock, [ny, nx], block_size, NRsm


def phasecorr_reference(refImg0, maskSlope, smooth_sigma, yblock, xblock, pad_fft):
    """ computes taper and fft'ed reference image for phasecorr
    
    Parameters
    ----------

    refImg : 2D array, int16
        reference image

    Returns
    -------
    maskMul : 2D array
        mask that is multiplied to spatially taper

    maskOffset : 2D array
        shifts in x from cfRefImg to data for each frame

    cfRefImg : 2D array, complex64
        reference image fft'ed and complex conjugate and multiplied by gaussian
        filter in the fft domain with standard deviation 'smooth_sigma'
    

    """

    Ly, Lx = refImg0.shape
    maskMul = spatial_taper(maskSlope, Ly, Lx)

    # split refImg0 into multiple parts
    nb = len(yblock)

    #patch taper
    Ly = yblock[0][1] - yblock[0][0]
    Lx = xblock[0][1] - xblock[0][0]
    cfRefImg1 = np.zeros((nb,1,next_fast_len(Ly), next_fast_len(Lx)), 'complex64') if pad_fft else np.zeros((nb, 1, Ly, Lx), 'complex64')
    maskMul1 = np.zeros((nb,1,Ly,Lx),'float32')
    maskOffset1 = np.zeros((nb,1,Ly,Lx),'float32')
    for n in range(nb):
        yind = yblock[n]
        yind = np.arange(yind[0], yind[-1]).astype('int')
        xind = xblock[n]
        xind = np.arange(xind[0], xind[-1]).astype('int')

        refImg = refImg0.squeeze()[np.ix_(yind,xind)]
        maskMul2 = spatial_taper(2 * smooth_sigma, Ly, Lx)
        maskMul1[n, 0, :, :] = maskMul[np.ix_(yind,xind)].astype('float32')
        maskMul1[n, 0, :, :] *= maskMul2.astype('float32')
        maskOffset1[n, 0, :, :] = (refImg.mean() * (1. - maskMul1[n, 0, :, :])).astype(np.float32)
        cfRefImg = np.conj(fft.fft2(refImg))
        absRef = np.absolute(cfRefImg)
        cfRefImg = cfRefImg / (1e-5 + absRef)

        # gaussian filter
        fhg = gaussian_fft(smooth_sigma, cfRefImg.shape[0], cfRefImg.shape[1])
        cfRefImg *= fhg
        cfRefImg1[n, 0, :, :] = cfRefImg.astype('complex64')

    return maskMul1, maskOffset1, cfRefImg1


def getSNR(cc, Ls):
    """ compute SNR of phase-correlation - is it an accurate predicted shift? """
    (lcorr, lpad) = Ls
    nimg = cc.shape[0]
    cc0 = cc[:, lpad:-lpad, lpad:-lpad]
    cc0 = np.reshape(cc0, (nimg, -1))
    X1max  = np.amax(cc0, axis=1)
    ix = np.argmax(cc0, axis=1)
    ymax, xmax = np.unravel_index(ix, (2 * lcorr + 1, 2 * lcorr + 1))
    # set to 0 all pts +-lpad from ymax,xmax
    cc0 = cc.copy()
    for j in range(nimg):
        cc0[j, ymax[j]:ymax[j] + 2 * lpad, xmax[j]:xmax[j] + 2 * lpad] = 0
    cc0 = np.reshape(cc0, (nimg, -1))
    Xmax = np.maximum(0, np.amax(cc0, axis=1))
    snr = X1max / Xmax  # computes snr
    return snr


def phasecorr(data, maskMul, maskOffset, cfRefImg, snr_thresh, NRsm, xblock, yblock, maxregshiftNR, subpixel: int = 10, lpad: int = 3):
    """ compute phase correlations for each block 
    
    Parameters
    -------------

    data : int16 or float32, 3D array
        size [nimg x Ly x Lx]

    refAndMasks : list
        gaussian filter, mask offset, FFT of reference image

    ymax1 : 2D array
        size [nimg x nblocks], y shifts of blocks

    xmax1 : 2D array
        size [nimg x nblocks], y shifts of blocks

    cmax1 : 2D array
        size [nimg x nblocks], value of peak of phase correlation

    ccsm : 4D array
        size [nimg x nblocks x ly x lx], smoothed phase correlations

    lpad: int
        upsample from a square +/- lpad
    """
    Kmat, nup = mat_upsample(lpad=3)

    nimg = data.shape[0]
    ly, lx = cfRefImg.shape[-2:]

    # maximum registration shift allowed
    maxregshift = np.round(maxregshiftNR)
    lcorr = int(np.minimum(maxregshift, np.floor(np.minimum(ly, lx) / 2.) - lpad))
    nb = len(yblock)

    # shifts and corrmax
    Y = np.zeros((nimg, nb, ly, lx), 'int16')
    for n in range(nb):
        yind, xind = yblock[n], xblock[n]
        Y[:,n] = data[:, yind[0]:yind[-1], xind[0]:xind[-1]]
    Y = addmultiply(Y, maskMul, maskOffset)
    fft2(Y, overwrite_x=True)
    Y = apply_dotnorm(Y, cfRefImg)
    ifft2(Y, overwrite_x=True)

    # calculate ccsm
    lhalf = lcorr + lpad
    cc0 = np.real(
        np.block(
            [[Y[:, :, -lhalf:,    -lhalf:], Y[:, :, -lhalf:,    :lhalf + 1]],
             [Y[:, :, :lhalf + 1, -lhalf:], Y[:, :, :lhalf + 1, :lhalf + 1]]]
        )
    )
    cc0 = cc0.transpose(1, 0, 2, 3)
    cc0 = cc0.reshape(cc0.shape[0], -1)

    cc2 = [cc0, NRsm @ cc0, NRsm @ NRsm @ cc0]
    cc2 = [c2.reshape(nb, nimg, 2 * lcorr + 2 * lpad + 1, 2 * lcorr + 2 * lpad + 1) for c2 in cc2]
    ccsm = cc2[0]
    for n in range(nb):
        snr = np.ones(nimg, 'float32')
        for j, c2 in enumerate(cc2):
            ism = snr < snr_thresh
            if np.sum(ism) == 0:
                break
            cc = c2[n, ism, :, :]
            if j > 0:
                ccsm[n, ism, :, :] = cc
            snr[ism] = getSNR(cc, (lcorr, lpad))

    # calculate ymax1, xmax1, cmax1
    ymax1 = np.zeros((nimg, nb), np.float32)
    cmax1 = np.zeros((nimg, nb), np.float32)
    xmax1 = np.zeros((nimg, nb), np.float32)
    ymax = np.zeros((nb,), np.int32)
    xmax = np.zeros((nb,), np.int32)
    for t in range(nimg):
        ccmat = np.zeros((nb, 2*lpad+1, 2*lpad+1), np.float32)
        for n in range(nb):
            ix = np.argmax(ccsm[n, t][lpad:-lpad, lpad:-lpad], axis=None)
            ym, xm = np.unravel_index(ix, (2 * lcorr + 1, 2 * lcorr + 1))
            ccmat[n] = ccsm[n, t][ym:ym + 2 * lpad + 1, xm:xm + 2 * lpad + 1]
            ymax[n], xmax[n] = ym - lcorr, xm - lcorr
        ccb = ccmat.reshape(nb, -1) @ Kmat
        cmax1[t] = np.amax(ccb, axis=1)
        ymax1[t], xmax1[t] = np.unravel_index(np.argmax(ccb, axis=1), (nup, nup))
        mdpt = nup // 2
        ymax1[t] = (ymax1[t] - mdpt) / subpixel + ymax
        xmax1[t] = (xmax1[t] - mdpt) / subpixel + xmax

    return ymax1, xmax1, cmax1, ccsm


@njit(['(int16[:, :],float32[:,:], float32[:,:], float32[:,:])', 
        '(float32[:, :],float32[:,:], float32[:,:], float32[:,:])'], cache=True)
def map_coordinates(I, yc, xc, Y):
    """ bilinear transform of image with ycoordinates yc and xcoordinates xc to Y 
    
    Parameters
    -------------

    I : int16 or float32, 2D array
        size [Ly x Lx]     

    yc : 2D array
        size [Ly x Lx], new y coordinates

    xc : 2D array
        size [Ly x Lx], new x coordinates

    Returns
    -----------

    Y : float32, 2D array
        size [Ly x Lx], shifted I


    """
    Ly,Lx = I.shape
    yc_floor = yc.astype(np.int32)
    xc_floor = xc.astype(np.int32)
    yc = yc - yc_floor
    xc = xc - xc_floor
    for i in range(yc_floor.shape[0]):
        for j in range(yc_floor.shape[1]):
            yf = min(Ly-1, max(0, yc_floor[i,j]))
            xf = min(Lx-1, max(0, xc_floor[i,j]))
            yf1= min(Ly-1, yf+1)
            xf1= min(Lx-1, xf+1)
            y = yc[i,j]
            x = xc[i,j]
            Y[i,j] = (np.float32(I[yf, xf]) * (1 - y) * (1 - x) +
                      np.float32(I[yf, xf1]) * (1 - y) * x +
                      np.float32(I[yf1, xf]) * y * (1 - x) +
                      np.float32(I[yf1, xf1]) * y * x )


@njit(['int16[:, :,:], float32[:,:,:], float32[:,:,:], float32[:,:], float32[:,:], float32[:,:,:]',
       'float32[:, :,:], float32[:,:,:], float32[:,:,:], float32[:,:], float32[:,:], float32[:,:,:]'], parallel=True, cache=True)
def shift_coordinates(data, yup, xup, mshy, mshx, Y):
    """ shift data into yup and xup coordinates

    Parameters
    -------------

    data : int16 or float32, 3D array
        size [nimg x Ly x Lx]     

    yup : 3D array
        size [nimg x Ly x Lx], y shifts for each coordinate

    xup : 3D array
        size [nimg x Ly x Lx], x shifts for each coordinate

    mshy : 2D array
        size [Ly x Lx], meshgrid in y

    mshx : 2D array
        size [Ly x Lx], meshgrid in x
        
    Returns
    -----------
    Y : float32, 3D array
        size [nimg x Ly x Lx], shifted data

    """
    for t in prange(data.shape[0]):
        map_coordinates(data[t], mshy+yup[t], mshx+xup[t], Y[t])


@njit((float32[:, :,:], float32[:,:,:], float32[:,:], float32[:,:], float32[:,:,:], float32[:,:,:]), parallel=True, cache=True)
def block_interp(ymax1, xmax1, mshy, mshx, yup, xup):
    """ interpolate from ymax1 to mshy to create coordinate transforms """
    for t in prange(ymax1.shape[0]):
        map_coordinates(ymax1[t], mshy, mshx, yup[t])  # y shifts for blocks to coordinate map
        map_coordinates(xmax1[t], mshy, mshx, xup[t])  # x shifts for blocks to coordinate map


def upsample_block_shifts(Lx, Ly, nblocks, xblock, yblock, ymax1, xmax1):
    """ upsample blocks of shifts into full pixel-wise maps for shifting

    this function upsamples ymax1, xmax1 so that they are nimg x Ly x Lx
    for later bilinear interpolation
        

    Parameters
    ------------

    ymax1 : 2D array
        size [nimg x nblocks], y shifts of blocks

    xmax1 : 2D array
        size [nimg x nblocks], y shifts of blocks
    
    Returns
    -----------

    yup : 3D array
        size [nimg x Ly x Lx], y shifts for each coordinate

    xup : 3D array
        size [nimg x Ly x Lx], x shifts for each coordinate

    """
    # make arrays of control points for piecewise-affine transform
    # includes centers of blocks AND edges of blocks
    # note indices are flipped for control points
    # block centers
    yb = np.array(yblock[::nblocks[1]]).mean(axis=1)  # todo: find out why yb has two colons and xb has one colon
    xb = np.array(xblock[:nblocks[1]]).mean(axis=1)

    iy = np.interp(np.arange(Ly), yb, np.arange(yb.size)).astype(np.float32)
    ix = np.interp(np.arange(Lx), xb, np.arange(xb.size)).astype(np.float32)
    mshx, mshy = np.meshgrid(ix, iy)

    # interpolate from block centers to all points Ly x Lx
    nimg = ymax1.shape[0]
    ymax1 = ymax1.reshape(nimg, nblocks[0], nblocks[1])
    xmax1 = xmax1.reshape(nimg, nblocks[0], nblocks[1])
    yup = np.zeros((nimg, Ly, Lx), np.float32)
    xup = np.zeros((nimg, Ly, Lx), np.float32)

    block_interp(ymax1, xmax1, mshy, mshx, yup, xup)

    return yup, xup


def transform_data(data, nblocks, xblock, yblock, ymax1, xmax1):
    """ piecewise affine transformation of data using block shifts ymax1, xmax1 
    
    Parameters
    -------------

    data : int16 or float32, 3D array
        size [nimg x Ly x Lx]

    ymax1 : 2D array
        size [nimg x nblocks], y shifts of blocks

    xmax1 : 2D array
        size [nimg x nblocks], y shifts of blocks

    Returns
    -----------
    Y : float32, 3D array
        size [nimg x Ly x Lx], shifted data

    """
    _, Ly, Lx = data.shape
    yup, xup = upsample_block_shifts(
        Lx=Lx,
        Ly=Ly,
        nblocks=nblocks,
        xblock=xblock,
        yblock=yblock,
        ymax1=ymax1,
        xmax1=xmax1,
    )

    # use shifts and do bilinear interpolation
    mshx, mshy = np.meshgrid(np.arange(Lx, dtype=np.float32), np.arange(Ly, dtype=np.float32))
    Y = np.zeros_like(data, dtype=np.float32)
    shift_coordinates(data, yup, xup, mshy, mshx, Y)
    return Y

