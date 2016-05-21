import os
import atexit
import cPickle as pickle
import numpy as np
from multiprocessing import cpu_count
from numpy.polynomial.legendre import legval
from numpy.polynomial.chebyshev import chebval

from peri.comp import Component
from peri.util import Tile, cdd, memoize, listify
from peri.fft import fft, rfft

#=============================================================================
# Begin 3-dimensional point spread functions
#=============================================================================
class PSF(Component):
    category = 'psf'

    def __init__(self, shape, params, values):
        """
        Point spread function classes must contain the following classes in order
        to interface with the states class:

            get_padding_size(z) : get the psf size at certain z position
            get_params : return the parameters of the psf
            set_tile : set the current update tile size
            update : update the psf based on new parameters
            execute : apply the psf to an image
        """
        self.shape = shape
        self.tile = Tile((0,0,0))
        super(PSF, self).__init__(params, values)

        self.update(params, values)
        self.set_tile(Tile(self.shape))

    @memoize()
    def calculate_kpsf(self, shape):
        d = ((shape - self.min_support))

        # fix off-by-one issues when going odd to even tile sizes
        o = d % 2
        d /= 2

        pad = tuple((d[i],d[i]+o[i]) for i in [0,1,2])
        self.rpsf = np.pad(self.min_rpsf, pad, mode='constant', constant_values=0)
        self.rpsf = np.fft.ifftshift(self.rpsf)
        self.kpsf = fft.fftn(self.rpsf)
        self.kpsf /= (np.real(self.kpsf[0,0,0]) + 1e-15)
        return self.kpsf

    def calculate_min_rpsf(self):
        # calculate the minimum supported real-space PSF
        min_support = np.ceil(self.get_padding_size()).astype('int')
        min_support += min_support % 2
        min_rpsf = self.rpsf_func(self._rvecs(min_support))
        return min_rpsf, min_support

    def set_tile(self, tile):
        if any(tile.shape < self.min_support):
            raise IndexError("PSF tile size is less than minimum support size")

        if (self.tile.shape != tile.shape).any():
            self.tile = tile

        self.kpsf = self.calculate_kpsf(self.tile.shape)

    def update(self, params, values):
        self.set_values(params, values)
        self.min_rpsf, self.min_support = self.calculate_min_rpsf()

        # clean out the cache since it is no longer useful
        if hasattr(self, '_memoize_clear'):
            self._memoize_clear()

    def execute(self, field):
        if any(field.shape != self.tile.shape):
            raise AttributeError("Field passed to PSF incorrect shape")

        if not np.iscomplex(field.ravel()[0]):
            infield = fft.fftn(field)
        else:
            infield = field

        return np.real(fft.ifftn(infield * self.kpsf))

    def get_padding_size(self, z=None):
        raise NotImplemented('subclasses must implement `get_padding_size`')

    def _rvecs(self, shape, centered=True):
        tile = Tile(shape)
        return tile.kvectors(norm=1.0/tile.shape, shift=centered)

    def __getstate__(self):
        odict = self.__dict__.copy()
        cdd(odict, ['_memoize_clear', '_memoize_caches'])
        cdd(odict, ['rpsf', 'kpsf', 'min_rpsf'])
        return odict

    def __setstate__(self, idict):
        self.__dict__.update(idict)
        self.tile = Tile((0,0,0))
        self.update(self.params, self.values)
        self.set_tile(Tile(self.shape))

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return str(self.__class__.__name__)+" {} ".format(self.values)

class IdentityPSF(PSF):
    def __init__(self):
        """
        Delta-function PSF; returns the field passed to execute identically. 
        Params is an N-element numpy.ndarray, doesn't do anything. 
        """
        super(IdentityPSF, self).__init__(shape=(1,)*3, params=['psf'], values=[0])

    def execute(self, field):
        return field
    
    def get_padding_size(self, *args):
        return np.ones(3)
    
    def update(self, params, values):
        self.set_values(params, values)
    
    def set_tile(self, tile):
        if (self.tile.shape != tile.shape).any():
            self.tile = tile

class AnisotropicGaussian(PSF):
    def __init__(self, shape, sigmas, error=1.0/255):
        self.error = error
        params = ['psf-sig-z', 'psf-sig-rho'] 
        super(AnisotropicGaussian, self).__init__(
            shape=shape, params=params, values=sigmas
        )

    def rpsf_func(self, vecs):
        rz, ry, rx = vecs
        rhosq = rx**2 + ry**2

        vals = np.array(self.values)/2
        arg = np.exp(-(rhosq/vals[0]**2 + (rz/vals[1])**2)/2)
        return arg * (rhosq <= self.pr**2) * (np.abs(rz) <= self.pz)

    def get_padding_size(self, z=None):
        self.pr = np.sqrt(-2*np.log(self.error)*self.values[0]**2)
        self.pz = np.sqrt(-2*np.log(self.error)*self.values[1]**2)
        return np.array([self.pz, self.pr, self.pr])

class AnisotropicGaussianXYZ(PSF):
    def __init__(self, shape, sigmas, error=1.0/255):
        self.error = error
        params = ['psf-sigz', 'psf-sigy', 'psf-sigx']
        super(AnisotropicGaussianXYZ, self).__init__(
            shape=shape, params=params, values=sigmas
        )

    def rpsf_func(self, vecs):
        rz, ry, rx = vecs
        params = np.array(self.values)/2

        arg = np.exp(-(
            (rz/params[0])**2 + (ry/params[1])**2 + (rx/params[2])**2
        )/2)
        return arg * (
            (np.abs(rx) <= self.px) * (np.abs(ry) <= self.py) *
            (np.abs(rz) <= self.pz)
        )

    def get_padding_size(self, z=None):
        self.px = np.sqrt(-2*np.log(self.error)*self.values[2]**2)
        self.py = np.sqrt(-2*np.log(self.error)*self.values[1]**2)
        self.pz = np.sqrt(-2*np.log(self.error)*self.values[0]**2)
        return np.array([self.pz, self.py, self.px])


#=============================================================================
# Begin 4-dimensional point spread functions
#=============================================================================
class PSF4D(PSF):
    def __init__(self, shape, params, values):
        """
        4-dimensional Point-Spread-Function (PSF) is implemented by assuming
        that the there is only z-dependence of parameters (so that it can be
        separated into x-y and z parts).  Therefore, we keep track of 2D FFTs
        and X-Y psf functions that get convolved with FFTs.  Then, we manually
        convolve in the z direction

        The key variables are rpsf (2d) and kpsf (2d) which are used for the
        x-y convolution.  The z-convolution cannot be cached.
        """
        super(PSF4D, self).__init__(shape=shape, params=params, values=values)

    def rvecs(self, tile):
        rz, ry, rx = tile.kvectors(norm=1.0/tile.shape)
        return rx, ry

    def _zpos(self, tile):
        return np.arange(tile.l[0], tile.r[0]).astype('float')

    @memoize()
    def _calc_tile_2d_psf(self, tile):
        rpsf = np.zeros(shape=self.tile.shape)
        kpsf = np.zeros(shape=self.tile.shape, dtype='complex')

        vecs = self.rvecs(tile)
        zs = self._zpos(tile)

        # calculate each slice from the rpsf_xy function
        for i,z in enumerate(zs):
            rpsf[i] = self.rpsf_xy(vecs, z)

        # calcualte the psf in k-space using 2d ffts
        kpsf = fft.fft2(rpsf)

        # need to normalize each x-y slice individually
        for i,z in enumerate(zs):
            kpsf[i,0,0] = 1.0

        return rpsf, kpsf

    def set_tile(self, tile):
        if (self.tile.shape != tile.shape).any():
            self.tile = tile

        self.rpsf, self.kpsf = self._calc_tile_2d_psf(self.tile)

    def update(self, params, values):
        # what should we update when the parameters are adjusted for
        # the 4d psf?  Well, for simplicity, let's start with nothing.
        self.set_values(params, values)

        # clean out the cache since it is no longer useful
        if hasattr(self, '_memoize_clear'):
            self._memoize_clear()

    def execute(self, field):
        if any(field.shape != self.tile.shape):
            raise AttributeError("Field passed to PSF incorrect shape")

        if not np.iscomplexobj(field):
            infield = fft.fft2(field)
        else:
            infield = field

        cov2d = np.real(fft.ifft2(infield * self.kpsf))
        cov2dT = np.rollaxis(cov2d, 0, 3)

        out = np.zeros_like(cov2d)
        z = self._zpos(self.tile)

        for i in xrange(len(z)):
            size = self.get_padding_size(z=z[i])
            m = (z >= z[i]-size[0]) & (z <= z[i]+size[0])
            g = self.rpsf_z(z[m], z[i])
            out[i] = cov2dT[...,m].dot(g)

        return out

    def rpsf_xy(self, vecs, z):
        """
        Returns the x-y plane real space psf function as a function of z values.
        This function does not necessarily have to be normalized, it will be
        normalized in k-space layer by layer later.
        """
        pass

    def rpsf_z(self, z, zp):
        """
        Returns the z dependence of the PSF.  This section needs to be noramlized.
        """
        pass

class Gaussian4D(PSF4D):
    def __init__(self, shape, sigmas=(2.0,0.5,1.0), order=(1,1,1),
            error=1.0/255, zrange=128):
        self.order = order
        self.error = error
        self.zrange = float(zrange)

        self.coeffs = {}
        params, values = [], []
        for i, o in enumerate(order):
            d = ['z', 'y', 'x']
            tparams = ['psf-%s-%i' % (d[i], j) for j in xrange(o)]
            tvalues = [sigmas[i]] + [0]*(o-1)

            params.extend(tparams)
            values.extend(tvalues)
            self.coeffs[i] = tparams

        super(Gaussian4D, self).__init__(shape=shape, params=params, values=values)

    def _sigma_coeffs(self, d=0):
        return listify(self.get_values(self.coeffs[d]))

    def _poly(self, z, coeffs):
        return np.polyval(coeffs[::-1], z)

    @memoize()
    def _sigma(self, z, d=0):
        return self._poly(z/self.zrange, self._sigma_coeffs(d=d))

    @memoize()
    def get_padding_size(self, z):
        if isinstance(z, np.ndarray) and z.shape[0] > 1:
            z = z[0]
        s = np.array([self._sigma(z, 0), self._sigma(z, 1), self._sigma(z, 2)])
        self.pz = np.max([np.sqrt(-2*np.log(self.error)*s[0]**2), 2.1*np.ones_like(s[0])], axis=0)
        self.py = np.max([np.sqrt(-2*np.log(self.error)*s[1]**2), 2.1*np.ones_like(s[1])], axis=0)
        self.px = np.max([np.sqrt(-2*np.log(self.error)*s[2]**2), 2.1*np.ones_like(s[2])], axis=0)
        size = np.array([self.pz, self.py, self.px])
        return size

    @memoize()
    def rpsf_z(self, z, zp):
        s = self._sigma(zp, 0)
        size = self.get_padding_size(z=zp)
        out = np.exp(-(z-zp)**2 / (2*s**2)) * (np.abs(z-zp) <= size[0])
        return out / out.sum()

    def rpsf_xy(self, vecs, zp):
        rx, ry = vecs
        size = self.get_padding_size(z=zp)
        mask = (np.abs(rx) <= size[2]) * (np.abs(ry) <= size[1])

        sx = self._sigma(zp, 2)
        sy = self._sigma(zp, 1)
        gauss = np.exp(-(rx/sx)**2/2-(ry/sy)**2/2)

        return gauss * mask

class Gaussian4DPoly(Gaussian4D):
    def __init__(self, shape, sigmas=(2.0,0.5,1.0), order=(1,1,1),
            error=1.0/255, zrange=128):
        super(Gaussian4DPoly, self).__init__(
            shape=shape, sigmas=sigmas, order=order, error=error, zrange=zrange
        )

    def _sigma_coeffs(self, d=0):
        return listify(self.get_values(self.coeffs[d]))

    def _poly(self, z, coeffs):
        return np.polyval(coeffs[::-1], z)

    @memoize()
    def _sigma(self, z, d=0):
        return self._poly(z/self.zrange, self._sigma_coeffs(d=d))

class Gaussian4DLegPoly(Gaussian4DPoly):
    def __init__(self, shape, sigmas=(2.0,0.5,1.0), order=(1,1,1),
            error=1.0/255, zrange=128):
        super(Gaussian4DLegPoly, self).__init__(
            shape=shape, sigmas=sigmas, order=order, error=error, zrange=zrange
        )

    def _poly(self, z, coeffs):
        return legval(z, coeffs)

class GaussianMomentExpansion(PSF4D):
    def __init__(self, shape, sigmas=(2.0,0.5,1.0), order=(1,1,1),
            moment_order=(3,3), error=1.0/255, zrange=128):
        """
        3+1D PSF that is of the form:
            (1+a*(3x-x^3) + b*(3-6*x^2+x^4))*exp(-(x/s)^2/2)
        where s is sigma, the scale factor.  
        """
        self.order = order
        self.morder = moment_order
        self.error = error
        self.zrange = float(zrange)

        self.poly_coeffs = {}
        self.moment_coeffs = {}

        params, values = [], []
        for i, o in enumerate(order):
            d = ['z', 'y', 'x']
            tparams = ['psf-%s-%i' % (d[i], j) for j in xrange(o)]
            tvalues = [sigmas[i]] + [0]*(o-1)

            params.extend(tparams)
            values.extend(tvalues)
            self.poly_coeffs[i] = tparams

        for i, o in enumerate(moment_order):
            t = ['skew', 'kurt']
            self.moment_coeffs[t[i]] = {}

            for j, d in enumerate(['z', 'y', 'x']):
                tparams = [
                    'psf-%s-%s-%i' % (t[i], d, k) for k in xrange(o)
                ]
                tvalues = [0]*(o)

                params.extend(tparams)
                values.extend(tvalues)
                self.moment_coeffs[t[i]][j] = tparams

        super(GaussianMomentExpansion, self).__init__(
            shape=shape, params=params, values=values
        )

    def _sigma_coeffs(self, d=0):
        return listify(self.get_values(self.poly_coeffs[d]))

    def _poly(self, z, coeffs):
        return np.polyval(coeffs[::-1], z)

    @memoize()
    def _sigma(self, z, d=0):
        return self._poly(z/self.zrange, self._sigma_coeffs(d=d))

    @memoize()
    def _moment(self, x, z, d=0):
        return (1+self._skew(x,z,d=d)+self._kurtosis(x, z, d=d))

    @memoize()
    def _kurtosis_coeffs(self, d):
        return listify(self.get_values(self.moment_coeffs['kurt'][d]))

    @memoize()
    def _skew_coeffs(self, d):
        return listify(self.get_values(self.moment_coeffs['skew'][d]))

    @memoize()
    def _skew(self, x, z, d=0):
        """ returns the kurtosis parameter for direction d, d=0 is rho, d=1 is z """
        # get the top bound determined by the kurtosis
        kval = (np.tanh(self._poly(z, self._kurtosis_coeffs(d)))+1)/12.
        bdpoly = np.array([
            -1.142468e+04,  3.0939485e+03, -2.0283568e+02,
            -2.1047846e+01, 3.79808487e+00, 1.19679781e-02
        ])
        top = np.polyval(bdpoly, kval)

        # limit the skewval to be 0 -> top val
        skew = self._poly(z, self._skew_coeffs(d))
        skewval = top*(np.tanh(skew) + 1) - top

        return skewval*(3*x - x**3)

    @memoize()
    def _kurtosis(self, x, z, d=0):
        """ returns the kurtosis parameter for direction d, d=0 is rho, d=1 is z """
        val = self._poly(z, self._kurtosis_coeffs(d))
        return (np.tanh(val)+1)/12.*(3 - 6*x**2 + x**4)

    @memoize()
    def get_padding_size(self, z):
        if isinstance(z, np.ndarray) and z.shape[0] > 1:
            z = z[0]
        s = np.array([self._sigma(z, 0), self._sigma(z, 1), self._sigma(z, 2)])
        self.pz = np.max([np.sqrt(-2*np.log(self.error)*s[0]**2), 2.1*np.ones_like(s[0])], axis=0)
        self.py = np.max([np.sqrt(-2*np.log(self.error)*s[1]**2), 2.1*np.ones_like(s[1])], axis=0)
        self.px = np.max([np.sqrt(-2*np.log(self.error)*s[2]**2), 2.1*np.ones_like(s[2])], axis=0)
        size = np.array([self.pz, self.py, self.px])
        return size

    @memoize()
    def rpsf_z(self, z, zp):
        s = self._sigma(zp, 0)
        size = self.get_padding_size(z=zp)
        pref = self._moment((z-zp)/s, zp, d=1)
        out = pref*np.exp(-(z-zp)**2 / (2*s**2)) * (np.abs(z-zp) <= size[0])
        return out / out.sum()

    def rpsf_xy(self, vecs, zp):
        rx, ry = vecs
        size = self.get_padding_size(z=zp)
        mask = (np.abs(rx) <= size[2]) * (np.abs(ry) <= size[1])

        sx = self._sigma(zp, 2)
        sy = self._sigma(zp, 1)
        rho = np.sqrt((rx/sx)**2 + (ry/sy)**2)
        gauss = self._moment(rho, zp, d=0)*np.exp(-rho**2/2)

        return gauss * mask

#=============================================================================
# Array-based specification of PSF
#=============================================================================
class FromArray(PSF):
    def __init__(self, array, *args, **kwargs):
        """
        Only thing to pass is the values of the point spread function (does not
        need to be normalized) in the form of a numpy ndarray of shape

        (z', z, y, x)

        so if there are 50 layers in the image and the psf is at most 16 wide
        then it must be shaped (50,16,16,16). The values of the psf must
        be centered in this array. Hint: np.fft.fftfreq provides the correct
        ordering of values for both even and odd lattices.
        """
        self.param_shape = array.shape
        self.support = np.array(array.shape[1:])
        super(FromArray, self).__init__(*args, params=array.flatten(), **kwargs)

    def set_tile(self, tile):
        if (self.tile.shape != tile.shape).any():
            self.tile = tile

    def _pad(self, field):
        if any(self.tile.shape < self.get_padding_size()):
            raise IndexError("PSF tile size is less than minimum support size")

        d = self.tile.shape - self.get_padding_size()

        # fix off-by-one issues when going odd to even tile sizes
        o = d % 2
        d /= 2

        pad = tuple((d[i],d[i]+o[i]) for i in [0,1,2])
        rpsf = np.pad(field, pad, mode='constant', constant_values=0)
        rpsf = np.fft.ifftshift(rpsf)
        kpsf = fft.fftn(rpsf)
        kpsf /= (np.real(kpsf[0,0,0]) + 1e-15)
        return kpsf

    def update(self, params):
        pass

    def execute(self, field):
        if any(field.shape != self.tile.shape):
            raise AttributeError("Field passed to PSF incorrect shape")

        if not np.iscomplex(field.ravel()[0]):
            infield = fft.fftn(field)
        else:
            infield = field

        outfield = np.zeros_like(infield, dtype='float')

        for i in xrange(field.shape[0]):
            z = int(self.tile.l[0] + i)
            kpsf = self._pad(self.params.reshape(self.param_shape)[z])
            outfield[i] = np.real(fft.ifftn(infield * kpsf))[i]

        return outfield

    def get_params(self):
        return self.params

    def get_padding_size(self, z=None):
        return self.support
