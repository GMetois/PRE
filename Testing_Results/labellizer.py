import PIL
import sys
import numpy as np
import cv2
import utilities.frames as frames
import visualparams as viz
from depth import utils as depth
import os

IMAGE_H, IMAGE_W = 1080, 1920

def get_corners(x, y) :
    """
    Function that gives the corners of a cell in the costmap

    args :
        x , y = coordinates of the cell with the altitude set to 0
    returns :
        points = a list a 4 points (x,y)
    """

    points = np.array([[x, y, 0], [x+1, y, 0], [x+1, y+1, 0], [x, y+1, 0]])

    return(points)

def correct_points(points_image):
    """Remove the points which are outside the image
    Args:
        points_image (ndarray (n, 2)): Points coordinates in the image plan
    Returns:
        points_image but the points outside the image are now on the edge of the image
    """
    # Keep only points which are on the image
    result = np.copy(points_image)
    for i in range(len(result)) :
        if result[i, 0] < 0 :
            result[i, 0] = 0
        if result[i, 0] > IMAGE_W :
            result[i, 0] = IMAGE_W
        if result[i, 1] < 0 :
            result[i, 1] = 0
        if result[i, 1] > IMAGE_H :
            result[i, 1] = IMAGE_H
    
    return result

def get_lists() :
    """
    Setup the list of rectangles and lines that will be later used of inference
    Args:
        Nothing : O_o
    Returns:
        rectangle_list : a list of X*Y coordinates of the rectangle position indicating where to crop
        grid_list : a list of X*Y coordinates to indicate the visual projection of the costmap on the display
    """

    rectangle_list = np.zeros((viz.Y,viz.X,2,2), np.int32)
    grid_list = np.zeros((viz.Y,viz.X,4,2), np.int32)

    if viz.X % 2 == 1 :
        offset = viz.X // 2 + 0.5
    else :
        offset = (viz.X // 2)

    for x in range(viz.X):
        for y in range(viz.Y):
            #Get the list of coordinates of the corners in the costmap frame
            points_costmap = get_corners(x, y)
            points_robot = points_costmap - np.array([(offset, 0, 0)])
            
            # Strange computation because the robot frame has the x axis toward the back of the robot
            # and the y axis to ward the left.
            points_robot = points_robot[:, [1,0,2]]
            points_robot = points_robot * np.array([1,-1,1])

            # Switching from the costmap coordinates to the world coordinates using the resolution of the costmap.
            points_robot = points_robot * viz.RESOLUTION

            # Compute the points coordinates in the camera frame
            points_camera = frames.apply_rigid_motion(points_robot, viz.CAM_TO_ROBOT)

            # Compute the points coordinates in the image plan
            points_image = frames.camera_frame_to_image(points_camera, viz.K)
            grid_list[y,x] = points_image

            # Get the Area of the cell that is in the image
            intern_points = correct_points(points_image)
            intern_area = (intern_points[0,1] - intern_points[2,1]) * ((intern_points[1,0]-intern_points[0,0])+(intern_points[2,0]-intern_points[3,0]))/2

            # Get the Area of the cell in total
            area = (points_image[0,1] - points_image[2,1]) * ((points_image[1,0]-points_image[0,0])+(points_image[2,0]-points_image[3,0]))/2

            # If the area in squared pixels of the costmap cell is big enough, then relevant data can be extracted
            #
            # IMPORTANT : If there's nothing to extract because the rectangle is too small on the image,
            # KEEP THE COORDINATES TO ZERO, this is the way we're going to check later for the pertinence of the coordinates.
            if intern_area/area >= viz.THRESHOLD_INTERSECT and area >= viz.THRESHOLD_AREA :
                #We project the footprint of the robot as if it was centered on the cell
                #We then get the smallest bounding rectangle of the footprint to keep the terrain on wich
                #it would step over if it was on the costmap's cell
                
                #Getting the footprint coordinates
                centroid = np.mean(points_robot, axis=0)
                point_tl = centroid + [0, 0.5*viz.L_ROBOT, 0]
                point_br = centroid - [0, 0.5*viz.L_ROBOT, 0]
                
                #Projecting the footprint in the image frame
                point_tl = frames.apply_rigid_motion(point_tl, viz.CAM_TO_ROBOT)
                point_br = frames.apply_rigid_motion(point_br, viz.CAM_TO_ROBOT)
                point_tl = frames.camera_frame_to_image(point_tl, viz.K)
                point_br = frames.camera_frame_to_image(point_br, viz.K)
                
                #Extracting the parameters for the rectangle
                point_tl = point_tl[0]
                point_br = point_br[0]
                crop_width = np.int32(np.min([IMAGE_W,point_br[0]])-np.max([0,point_tl[0]]))
                crop_height = np.int32(crop_width//3)
                
                #Extracting the rectangle from the centroid of the projection of the costmap's cell
                centroid = np.mean(points_image, axis=0)
                tl_x = np.clip(centroid[0] - crop_width//2, 0, IMAGE_W-crop_width)
                tl_y = np.clip(centroid[1]-crop_height//2, 0, IMAGE_H-crop_height)
                rect_tl = np.int32([tl_x, tl_y])
                rect_br = rect_tl + [crop_width, crop_height]
                #Appending the rectangle to the list
                rectangle_list[y,x] = np.array([rect_tl, rect_br])

    return(rectangle_list, grid_list)

def enter_costs(img, rectangle_list):
    """
    The main function of this programs, take a list of coordinates and the input image
    Put them in the NN and compute the cost for each crop
    Then reconstituate a costmap of costs
    Args:
        img : RGB input of the robot
        img_depth : depth image of the robot
        img_normals : RGB representation of the normals computed from the depth image
        rectangle_list : list of the rectangle coordinates indicating where to crop according to the costmap's projection on the image
        model : the NN
    
    Returns:
        Costmap : A numpy array of X*Y dimension with the costs
        max_cost, min_cost : the max and min cost of the costmap, useful for visualization later ;)
    """
    #Intializing buffers
    costmap = np.zeros((viz.Y,viz.X))
    cv2.imshow("Img", cv2.resize(img, (960, 540)))

    #Iteratinf on the rectangles
    for x in range(viz.X):
        for y in range(viz.Y) :
            
            #Getting the rectangle coordinates
            rectangle = rectangle_list[y,x]
            #Cropping the images to get the inputs we want for this perticular cell
            crop = img[rectangle[0,1]:rectangle[1,1], rectangle[0,0]:rectangle[1,0]]
            #If the rectangle is not empty (Check if we considered beforehand that it was useful to crop there)
            if not np.array_equal(rectangle, np.zeros(rectangle.shape)) :
                #Converting the BGR image to RGB
                crop = cv2.cvtColor(np.uint8(crop), cv2.COLOR_BGR2RGB)
                
                cv2.imshow(f"{x}, {y} :", crop)
                cv2.waitKey(1000)
                while True :
                    try :
                        print("Enter the cost :")
                        cost = float(input())
                    except ValueError :
                        print("Error, try again")
                        continue
                    break
                cv2.destroyWindow(f"{x}, {y} :")
                
                
                #Filling the output array (the numeric costmap)
                costmap[y,x] = cost
    
    cv2.destroyWindow("Img")
    return(costmap)

def display(img, costmap, rectangle_list, grid_list, max_cost, min_cost) :
    """
    A function that displays what's currently computed
    Args :
        img : the base image
        costmap : the numerical costmap
        rectangle_list : the list of the coordinates of the cropping rectangles for the NN input
        gris_list : the list of the coordinates of the projected costmap's cells
        max_cost, min_cost : the max and min cost for the color gradient
    
    Returns :
        Displays a bunch of windows with funny colors on it, but nothing else.
    """
    #Buffers initialization
    imgviz = img.copy()
    costmapviz = np.zeros((viz.Y,viz.X,3), np.uint8)

    #For each costmap element
    for x in range(viz.X):
        for y in range(viz.Y):
            #Getting the rectangle coordinate
            rectangle = rectangle_list[y,x]
            #Checking if we estimated beforehand that the rectangle might have something interesting to display
            if not np.array_equal(rectangle, np.zeros(rectangle.shape)) :
                #If there's something we get the coordinates of the cell and the rectangle
                rect_tl, rect_br = rectangle[0], rectangle[1]
                points_image = grid_list[y,x]

                #Display the center of the cell
                centroid = np.mean(points_image, axis=0)
                cv2.circle(imgviz, tuple(np.int32(centroid)) , radius=4, color=(255,0,0), thickness=-1)
                
                # Displaying the rectangle
                rect_tl = tuple(rect_tl)
                rect_br = tuple(rect_br)
                cv2.rectangle(imgviz, rect_tl, rect_br, (255,0,0), 1)
                #Display the frontiers of the cell
                points_image_reshape = points_image.reshape((-1,1,2))
                cv2.polylines(imgviz,np.int32([points_image_reshape]),True,(0,255,255))

    #Building cell per cell and array that will become our costmap visualization
    for x in range(viz.X) :
        for y in range(viz.Y) :
            
            #If the cell is not empty because some cost has been generated
            if costmap[y,x]!= 0 :
                
                #Normalizing the content
                value = np.uint8(((costmap[y,x]-min_cost)/(max_cost-min_cost))*255)
                costmapviz[y,x] = (value, value, value)
            else :
                #If nothing we leave the image black
                costmapviz[y,x] = (0, 0, 0)
    #Applying the color gradient        
    costmapviz = cv2.applyColorMap(src=costmapviz, colormap=cv2.COLORMAP_JET)
    
    #Displaying the results
    cv2.imshow("Result", imgviz)
    cv2.imshow("Costmap", cv2.resize(cv2.flip(costmapviz, 0),(viz.X*20,viz.Y*20)))
    cv2.waitKey(0)

directory = "/home/gabriel/PRE/bagfiles/images_extracted/"

number_files = len([name for name in os.listdir("/home/gabriel/PRE/bagfiles/images_extracted/") if os.path.isfile(directory + name) and name.lower().endswith('.png')])//3

files = np.array([directory + f"{i+1}.png" for i in range(number_files)])

print(files)

np.save("/home/gabriel/PRE/bagfiles/images_extracted/files", files)

costmaps = np.zeros((number_files, viz.Y, viz.X))

rectangle_list, grid_list = get_lists()

for i in range(12,number_files) :
    img = cv2.imread(files[i])

    costmaps[i, :, :] = enter_costs(img, rectangle_list)
    np.save(f"/home/gabriel/PRE/bagfiles/images_extracted/costmaps{i+1}", costmaps[i])

#choices = np.random.choice(number_files, 4)

#for i in range(number_files) :
#    img = cv2.imread(files[i])
#    costmap = np.load(f"/home/gabriel/PRE/bagfiles/images_extracted/costmaps{i+1}.npy")
#    display(img, costmap, rectangle_list, grid_list, np.max(costmap), np.min(costmap))


