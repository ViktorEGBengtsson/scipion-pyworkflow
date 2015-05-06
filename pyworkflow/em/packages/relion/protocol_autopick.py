# **************************************************************************
# *
# * Authors:     J.M. De la Rosa Trevin (jmdelarosa@cnb.csic.es)
# *
# * Unidad de  Bioinformatica of Centro Nacional de Biotecnologia , CSIC
# *
# * This program is free software; you can redistribute it and/or modify
# * it under the terms of the GNU General Public License as published by
# * the Free Software Foundation; either version 2 of the License, or
# * (at your option) any later version.
# *
# * This program is distributed in the hope that it will be useful,
# * but WITHOUT ANY WARRANTY; without even the implied warranty of
# * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# * GNU General Public License for more details.
# *
# * You should have received a copy of the GNU General Public License
# * along with this program; if not, write to the Free Software
# * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA
# * 02111-1307  USA
# *
# *  All comments concerning this program package may be sent to the
# *  e-mail address 'jmdelarosa@cnb.csic.es'
# *
# **************************************************************************

import os
from os.path import exists
from pyworkflow.protocol.params import (PointerParam, FloatParam, StringParam, RelationParam,
                                        IntParam, BooleanParam, LEVEL_ADVANCED, 
                                        LabelParam)
from pyworkflow.em.data import Volume 
from pyworkflow.em.protocol.protocol_particles import ProtParticlePicking
from pyworkflow.em.constants import RELATION_CTF

from protocol_base import ProtRelionBase
from convert import writeSetOfMicrographs, writeReferences
from pyworkflow.em.convert import ImageHandler
from pyworkflow.utils.path import replaceBaseExt


class ProtRelionAutopickFom(ProtParticlePicking, ProtRelionBase):
    """    
    This Relion protocol uses 2D class averages as templates to run the auto-picking 
    job-type. In this first stage, the auto-picking will be run just in few micrographs 
    to optimise two of its main parameters ( _Picking threshold_ and _Minimum inter-particle distance_).
    
    In order to save time, only 2 or 3 micrographs should be used with their CTF 
    information. One should use representative micrographs for the entire data set, 
    e.g. a high and a low-defocus one, and/or with thin or thick ice. 

    The expensive part of this calculation is to calculate a probability-based figure-of-merit 
    (related to the cross-correlation coefficient between each rotated reference and all positions 
    in the micrographs. This calculation is followed by a much cheaper peak-detection algorithm that 
    uses the threshold and minimum distance parameters mentioned above. Because these parameters 
    need to be optimised, this first stage of the auto-picking will write out so-called FOM maps.
    These are two large (micrograph-sized) files per reference. To avoid running into hard disc I/O 
    problems, the autopicking program can only be run sequentially (hence there is no option to use 
    more than one single MPI processor).
    """
    _label = 'auto-picking FOM'
    
    #--------------------------- DEFINE param functions --------------------------------------------   
    def _defineParams(self, form):
        form.addSection(label='Input')
        
        form.addParam('inputMicrographs', PointerParam, pointerClass='SetOfMicrographs',
                      label='Input micrographs (a few)', important=True,
                      help='Select a set with just a few micrographs to be used\n'
                           'in the auto-picking job. A few should be used in order\n'
                           'to perform the expensive calculation of the figure-of-merits.\n'
                           'writing out the so-called FOM maps.')
        form.addParam('ctfRelations', RelationParam, allowsNull=True,
                      relationName=RELATION_CTF, attributeName='getInputMicrographs',
                      label='CTF estimation',
                      help='Choose some CTF estimation related to input micrographs. \n')
        
        #TODO: CHECK PARTICLE DIAMETER command line option used from General in Relion GUI

        form.addSection('References')
        form.addParam('inputReferences', PointerParam, pointerClass='SetOfAverages,SetOfClasses2D',
                      label='References', 
                      help='Input references (SetOfAverages or SetOfClasses2D\n'
                           'to be used for picking. \n\n'
                           'Note that the absolute greyscale needs to be correct, \n'
                           'so only use images with proper normalization.')
        form.addParam('lowpassFilterRefs', IntParam, default=20,
                      label='Lowpass filter references (A)',
                      help='Lowpass filter that will be applied to the references \n'
                           'before template matching. \n'
                           'Do NOT use very high-resolution templates to search your micrographs. \n'
                           'The signal will be too weak at high resolution anyway,\n' 
                           'and you may find Einstein from noise....')
        form.addParam('angularSampling', IntParam, default=5,
                      label='Angular sampling (deg)',
                      help='Angular sampling in degrees for exhaustive searches \n'
                           'of the in-plane rotations for all references.')
        form.addParam('refsHaveInvertedContrast', BooleanParam,
                      label='References have inverted contrast',
                      help='Set to Yes to indicate that the reference have inverted \n'
                           'contrast with respect to the particles in the micrographs.')
        form.addParam('refsCtfCorrected', BooleanParam, default=True,
                      label='Are References CTF corrected?',
                      help='Set to Yes if the references were created with CTF-correction inside RELION.\n'
                           'If set to Yes, the input micrographs should contain the CTF information.')
        form.addParam('ignoreCTFUntilFirstPeak', BooleanParam, default=False,
                      expertLevel=LEVEL_ADVANCED,
                      label='Ignore CTFs until first peak?',
                      help='Set this to Yes, only if this option was also used to generate the references.')
        
        form.addSection('Autopicking')
        form.addParam('pickingThreshold', FloatParam, default=0.05,
                      label='Picking threshold',
                      help='Use lower thresholds to pick more particles (and more junk probably)')
        form.addParam('interParticleDistance', IntParam, default=100,
                      label='Minimum inter-particle distance (A)',
                      help='Particles closer together than this distance \n'
                           'will be consider to be a single cluster. \n'
                           'From each cluster, only one particle will be picked.')
        form.addParam('fomLabel', LabelParam,  
                      label='FOM maps will be written to be used later.')
        form.addParam('extraParams', StringParam, default='',
              label='Additional parameters',
              help='')
        
    #--------------------------- INSERT steps functions --------------------------------------------  

    def _insertAllSteps(self): 
        self._insertFunctionStep('convertInputStep', self.inputMicrographs.get().strId())
        self._insertAutopickStep()
        self._insertFunctionStep('createOutputStep', 1)
        
    def _preprocessMicrographRow(self, img, imgRow):
        # Temporarly convert the few micrographs to tmp and make sure
        # they are in 'mrc' format
        # Get basename and replace extension by 'mrc'
        newName = replaceBaseExt(img.getFileName(), 'mrc')
        self._ih.convert(img, self._getExtraPath(newName))
        # The command will be launched from the working dir
        # so, let's make the micrograph path relative to that
        img.setFileName(os.path.join('extra', newName))
        img.setCTF(self.ctfDict[img.getMicName()])
            
    def convertInputStep(self, micsId):
        self._ih = ImageHandler() # used to convert micrographs
        # Match ctf information against the micrographs
        self.ctfDict = {}
        for ctf in self.ctfRelations.get():
            self.ctfDict[ctf.getMicrograph().getMicName()] = ctf.clone()
        
        micStar = self._getPath('input_micrographs.star')
        # TODO: add ctf information
        writeSetOfMicrographs(self.inputMicrographs.get(), micStar, 
                              preprocessImageRow=self._preprocessMicrographRow)
        # TODO: handle the case of classes2D
        writeReferences(self.inputReferences.get(), self._getPath('input_references'))
        
    def _insertAutopickStep(self):
        """ Prepare the command line for calling 'relion_autopick' program """
        micSet = self.inputMicrographs.get()
        
        params = ' --i input_micrographs.star'
        params += ' --o autopick'
        params += ' --particle_diameter %d' % 200 # FIXME
        params += ' --angpix %0.3f' % micSet.getSamplingRate()
        params += ' --ref input_references.star'
        
        if self.refsHaveInvertedContrast:
            params += ' --invert'
        
        if self.refsCtfCorrected:
            params += ' --ctf'
            
        params += ' --ang %d' % self.angularSampling
        params += ' --lowpass %d' % self.lowpassFilterRefs
        params += ' --threshold %0.3f' % self.pickingThreshold
        params += ' --min_distance %0.3f' % self.interParticleDistance
        
        #params += ' --write_fom_maps' 
        
        params += ' ' + self.extraParams.get('')
        
        self._insertFunctionStep('autopickStep', params)
        
    #--------------------------- STEPS functions --------------------------------------------
    def autopickStep(self, params):
        """ Launch the 'relion_autopick' with the given parameters. """
        self.runJob(self._getProgram('relion_autopick'), params, 
                    cwd=self.getWorkingDir())
    
    def createOutputStep(self, t):
        pass
    
    #--------------------------- INFO functions -------------------------------------------- 
    def _validate(self):
        """ Should be overriden in subclasses to 
        return summary message for NORMAL EXECUTION. 
        """
        errors = []
        return errors
    
    def _summary(self):
        """ Should be overriden in subclasses to 
        return summary message for NORMAL EXECUTION. 
        """
        return []
    
    #--------------------------- UTILS functions --------------------------------------------
